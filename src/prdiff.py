"""PR diff 획득 + 예산 패킹 (reviewer/verifier 공용).

두 가지를 처리한다:
  ① GitHub API는 20,000줄이 넘는 diff를 거부(HTTP 406)하므로, 그 경우 캐시 클론에서
     merge-base 기준으로 직접 계산해 폴백한다.
  ② 프롬프트 예산을 넘는 diff는 **파일 단위**로 담는다. 문자로 자르면 파일 중간에서
     끊겨 리뷰어가 무엇을 못 봤는지조차 모르기 때문. 빠진 파일은 매니페스트에 전부
     남겨 워크트리에서 직접 열게 한다.
"""
from . import db, ghclient, worktree
from .config import CFG

MAX_DIFF_CHARS = int(CFG.get("max_diff_chars", 120000))


def split_by_file(diff: str):
    """유니파이드 diff를 `diff --git` 경계로 [(path, chunk, added, deleted)] 분해."""
    files, path, buf, added, deleted = [], None, [], 0, 0
    in_hunk = False  # 헤더의 ---/+++ 와 본문의 '---' 삭제 줄을 구분하려면 필요

    def flush():
        if buf:
            files.append((path, "".join(buf), added, deleted))

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            path = line.split(" b/", 1)[-1].strip() if " b/" in line else line.strip()
            buf, added, deleted, in_hunk = [line], 0, 0, False
            continue
        buf.append(line)
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
    flush()
    return files


def pack(diff: str, budget: int = MAX_DIFF_CHARS):
    """(diff_text, manifest, omitted_count) — 파일 청크를 통째로 예산 안에 담는다.

    **추가 라인이 있는 파일(새 동작)** 에 예산을 먼저 주고, 삭제만 있는 파일은 남는
    예산으로 채운다. 대량 삭제 리팩터링에서 지워진 코드가 예산을 다 먹는 것을 막는다.
    """
    files = split_by_file(diff)
    if not files:
        return diff[:budget], "", 0

    total_a = sum(f[2] for f in files)
    total_d = sum(f[3] for f in files)
    if len(diff) <= budget:
        return diff, (f"{len(files)} files changed, +{total_a} / -{total_d} "
                      f"— 전체 diff가 위에 포함됨."), 0

    order = sorted(range(len(files)), key=lambda i: (files[i][2] == 0, i))
    kept, used = set(), 0
    for i in order:
        chunk = files[i][1]
        if used + len(chunk) > budget:
            continue
        kept.add(i)
        used += len(chunk)

    text = "".join(files[i][1] for i in range(len(files)) if i in kept)
    lines = [
        f"{len(files)} files changed, +{total_a} / -{total_d}. "
        f"**위 diff에는 {len(kept)}개 파일만 포함**됐다 (프롬프트 예산 초과). "
        f"`[미포함]` 파일은 diff가 없으니, 판단이 필요하면 워크트리에서 Read/Grep으로 직접 열 것.",
        "",
    ]
    for i, (path, _chunk, a, d) in enumerate(files):
        lines.append(f"{'[포함]  ' if i in kept else '[미포함]'} {path} (+{a}/-{d})")
    return text, "\n".join(lines), len(files) - len(kept)


def fetch(c, card) -> str:
    """패킹 전 raw diff. API가 거부하면 캐시 클론에서 로컬 계산으로 폴백.

    규모 판정(문서 리뷰 플래너의 large-PR 임계 등)은 패킹 후 길이로는 할 수 없어
    획득과 패킹을 나눠 둔다.
    """
    repo, pr, head = card["repo"], card["pr_number"], card["head_sha"]
    try:
        return ghclient.pr_diff(repo, pr)
    except ghclient.DiffTooLarge:
        base_ref = card["base_sha"] or "HEAD"
        diff = worktree.local_diff(repo, pr, head, base_ref)
        db.log_event(c, "diff_local_fallback", card["key"],
                     {"base": base_ref, "chars": len(diff)})
        return diff


def pack_logged(c, card, diff: str, budget: int = MAX_DIFF_CHARS):
    """(packed_diff, manifest) — 절삭이 일어나면 이벤트로 남긴다."""
    packed, manifest, omitted = pack(diff, budget)
    if omitted:
        db.log_event(c, "diff_truncated", card["key"],
                     {"omitted_files": omitted, "total_chars": len(diff), "budget": budget})
    return packed, manifest


def collect(c, card, budget: int = MAX_DIFF_CHARS):
    """(packed_diff, manifest) — fetch + pack 한 번에."""
    return pack_logged(c, card, fetch(c, card), budget)
