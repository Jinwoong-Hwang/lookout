#!/usr/bin/env python3
"""리뷰 파이프라인을 게시 없이 실제 PR 로 재현한다.

리뷰 로직을 고친 뒤 "정말 안 올라가는가"를 확인하는 자리다. 단위 테스트는 붙인
가짜 회신으로 돌지만, #10066 의 버그는 실제 댓글 모양(묶음·리뷰 본문·작성자
계정의 봇 글)에서만 드러났다. 그래서 진짜 PR, 진짜 엔진으로 돌리되 게시만 막는다.

안전장치 세 겹:
  1. 스크래치 DB — 라이브(~/hermes-pr/db)를 .backup 으로 복사해 그 사본만 건드린다
  2. dry_run_comments / dry_run_approve 강제
  3. ghclient.pr_comment·pr_approve 를 예외 함수로 교체 — dry-run 플래그가
     어디선가 꺼져 있어도 게시가 물리적으로 불가능하다
  그리고 실행 전후로 PR 의 코멘트 수와 updatedAt 을 대조해 무변경을 확인한다.

clone 캐시는 라이브와 공유한다(zigbang-client 는 1.2G 라 재클론이 비현실적).
worktree 는 스크래치에 만들고 끝나면 prune 한다.

엔진은 config.default_review_engine 을 따른다(--engine 으로 덮어쓴다). 두 엔진의
리뷰 프롬프트가 갈리고 closure 응답 형식도 엔진마다 다를 수 있어, 배포 전에는
양쪽 다 돌려보는 편이 안전하다.

사용:
  python3 replay-review.py zigbang/zigbang-client 10066
  python3 replay-review.py zigbang/zigbang-client 10066 --engine claude
  python3 replay-review.py zigbang/zigbang-client 10066 --second-pass

--second-pass 는 "커밋 하나 더 얹은 head" 를 흉내낸다. 1차 실행 뒤 '마지막으로
판정한 시점' 을 head 의 **실제 부모 커밋**으로 돌려놓고 한 번 더 돌린다. 가짜
sha 를 쓰면 두 커밋 사이 변경 파일을 계산할 수 없어 호출 게이트가 늘 '모름' 으로
빠지므로, 실제 조상이어야 한다. 이 경로로만 확인되는 것 둘 —
  · 작성자 회신이 한 번뿐인 상태에서 새 커밋이 와도 보류가 유지되는가
  · 달라진 게 없는 지적의 closure 호출을 실제로 건너뛰는가

종료 코드: 게시될 뻔한 것이 있으면 1, 아니면 0.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback

HOME = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOME)

from src import config  # noqa: E402




class StageFailed(RuntimeError):
    """단계가 깨졌다 — 뒤 단계·2차를 이어가면 같은 실패를 반복한다."""


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def gh_text(args):
    proc = subprocess.run([config.resolve_bin(config.CFG["gh_bin"]), *args],
                          capture_output=True, text=True, env=config.subprocess_env())
    if proc.returncode != 0:
        raise SystemExit(f"gh 실패: {' '.join(args)}\n{proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def gh_json(args):
    """`gh api -q` 는 스칼라를 따옴표 없이 뱉는다 — 객체를 뽑는 질의에만 쓸 것."""
    out = gh_text(args)
    return json.loads(out) if out else None


def pr_fingerprint(repo, pr):
    """게시 무변경을 확인할 지문 — 코멘트 수 + 마지막 갱신 시각."""
    info = gh_json(["api", f"repos/{repo}/pulls/{pr}",
                    "-q", "{updated_at, comments, review_comments}"])
    return info


def copy_live_db(dest):
    live = os.path.expanduser("~/hermes-pr/db/kanban.sqlite")
    if not os.path.isfile(live):
        live = config.path("db/kanban.sqlite")
    if os.path.exists(dest):
        os.remove(dest)
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    return live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("pr", type=int)
    # 기본값은 운영 설정을 따른다 — 검증은 실제로 도는 엔진으로 해야 의미가 있다.
    # claude 로만 확인하면 closure 계약(reply_comment_id + 정확한 연속 인용)을
    # 그 엔진이 지켰다는 증거일 뿐이고, 리뷰 프롬프트도 엔진별로 갈린다.
    default_engine = config.CFG.get("default_review_engine") or "codex"
    if default_engine not in ("claude", "codex"):
        default_engine = "codex"
    ap.add_argument("--engine", default=default_engine, choices=("claude", "codex"),
                    help=f"기본값은 config.default_review_engine (현재 {default_engine})")
    ap.add_argument("--workdir", default=os.path.join(HOME, ".replay"))
    ap.add_argument("--second-pass", action="store_true",
                    help="새 커밋이 온 상황을 흉내내 한 번 더 돌린다")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    db_path = os.path.join(args.workdir, "replay.sqlite")
    live = copy_live_db(db_path)
    log(f"DB 사본: {live} → {db_path}")

    # run-demo.py 와 같은 방식 — CFG 를 덮은 뒤에 import 해야 반영된다
    config.CFG["db_path"] = db_path
    config.CFG["worktree_dir"] = os.path.join(args.workdir, "worktrees")
    shared_repos = os.path.expanduser("~/hermes-pr/repos")
    if os.path.isdir(shared_repos):
        config.CFG["repo_cache_dir"] = shared_repos  # 1.2G 재클론 회피
    config.CFG["dry_run_comments"] = True
    config.CFG["dry_run_approve"] = True

    from src import commenter, db, ghclient, reviewer, verifier, worktree  # noqa: E402

    # 라이브 DB 사본은 그 시점 스키마 그대로다 — 운영은 tick 이 db.init() 으로
    # 마이그레이션하므로, 재현도 같은 단계를 거쳐야 새 칼럼이 생긴다.
    db.init()

    def blocked(*_a, **_k):
        raise AssertionError("replay: GitHub 쓰기 시도가 차단됐다")

    ghclient.pr_comment = blocked
    ghclient.pr_approve = blocked

    before = pr_fingerprint(args.repo, args.pr)
    head = gh_text(["api", f"repos/{args.repo}/pulls/{args.pr}", "-q", ".head.sha"])
    log(f"{args.repo}#{args.pr} @ {head[:10]} · engine={args.engine}")

    try:
        posted = run_pass(db, ghclient, reviewer, verifier, commenter, worktree,
                          args, head, label="1차")
    except StageFailed as e:
        log(f"!! {e} — 중단한다")
        return 3
    if args.second_pass:
        parent = gh_text(["api", f"repos/{args.repo}/commits/{head}",
                          "-q", ".parents[0].sha"])
        log("")
        log(f"=== 2차: 새 커밋이 온 상황 — 판정 시점을 부모 커밋 {parent[:10]} 로 ===")
        with db.connect() as c:
            n = c.execute(
                """UPDATE findings SET decision_head=? WHERE repo=? AND pr_number=?
                   AND decision_head IS NOT NULL AND decision_head!=''""",
                (parent, args.repo, args.pr)).rowcount
            m = c.execute(
                """UPDATE findings SET last_judged_head=? WHERE repo=? AND pr_number=?
                   AND last_judged_head IS NOT NULL AND last_judged_head!=''""",
                (parent, args.repo, args.pr)).rowcount
        changed = worktree.changed_files_between(args.repo, parent, head)
        log(f"결정 {n}건 · 판정시점 {m}건 되돌림 · 그 사이 변경 파일 "
            f"{len(changed) if changed is not None else '모름'}개")
        try:
            posted += run_pass(db, ghclient, reviewer, verifier, commenter, worktree,
                               args, head, label="2차")
        except StageFailed as e:
            log(f"!! {e} — 중단한다")
            return 3

    after = pr_fingerprint(args.repo, args.pr)
    log("")
    if before != after:
        log(f"!! PR 이 변했다 — before={before} after={after}")
        return 2
    log(f"PR 무변경 확인 — 코멘트 {after['comments']}건 · "
        f"리뷰코멘트 {after['review_comments']}건 · updated_at {after['updated_at']}")
    if posted:
        log(f"!! 게시될 뻔한 묶음 {posted}건")
        return 1
    log("게시될 것 없음")
    return 0


def run_pass(db, ghclient, reviewer, verifier, commenter, worktree, args, head, label):
    repo, pr = args.repo, args.pr
    key = f"replay:{repo}#{pr}:{label}:{head}"
    with db.connect() as c:
        base = c.execute(
            """SELECT * FROM cards WHERE repo=? AND pr_number=? AND kind='review'
               ORDER BY id DESC LIMIT 1""", (repo, pr)).fetchone()
        payload = json.loads(base["payload"]) if base and base["payload"] else {}
        payload.pop("force_post", None)
        payload.pop("intro", None)
        c.execute("DELETE FROM cards WHERE key=?", (key,))
        card_id = db.upsert_card(c, key, "review", repo, pr, "intake", head,
                                 base["base_sha"] if base else None, payload=payload)
        c.execute("UPDATE cards SET engine=? WHERE id=?", (args.engine, card_id))
        priors = db.prior_open_findings(c, repo, pr, card_id)
        log(f"[{label}] 카드 #{card_id} · 기존 지적 {len(priors)}건")
        for p in priors:
            log(f"    [{p['status']}] {p['fp'].rsplit(':', 1)[-1]}")

    for name, fn, want in (("reviewer", reviewer.process, "intake"),
                           ("verifier", verifier.process, "verifying"),
                           ("commenter", commenter.process, "commenting")):
        with db.connect() as c:
            card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            if card["status"] != want:
                log(f"[{label}] {name} 건너뜀 (status={card['status']})")
                continue
            t0 = time.time()
            try:
                fn(c, card)
            except Exception:
                log(f"[{label}] {name} 실패\n{traceback.format_exc()}")
                report(db, repo, pr, card_id, key, label)
                # 단계가 깨진 뒤 2차를 이어가면 같은 실패를 한 번 더 반복할 뿐이다
                raise StageFailed(f"{label} {name}")
            log(f"[{label}] {name} 완료 {time.time() - t0:.0f}s")

    return report(db, repo, pr, card_id, key, label)


def report(db, repo, pr, card_id, key, label):
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        log(f"[{label}] 카드 최종 상태: {card['status']}")

        skipped = c.execute(
            "SELECT COUNT(*) n FROM events WHERE key=? AND type='finding_closure_skipped'",
            (key,)).fetchone()["n"]
        log(f"[{label}] closure 판정 (건너뜀 {skipped}건)")
        for e in c.execute("SELECT detail FROM events WHERE key=? AND type='finding_closure'",
                           (key,)):
            d = json.loads(e["detail"])
            src = d.get("reply_comment_id") or "-"
            log(f"    {d['status']:<14} {d['fp'].rsplit(':', 1)[-1][:46]:<48} 근거={src}")

        log(f"[{label}] 지적 상태")
        for f in c.execute(
                """SELECT * FROM findings WHERE repo=? AND pr_number=?
                   AND status NOT IN ('resolved','rejected') ORDER BY id""", (repo, pr)):
            note = (f["decision_evidence"] or "").replace("\n", " ")[:60]
            log(f"    [{f['status']:<14}] {f['fp'].rsplit(':', 1)[-1][:46]}"
                + (f"  ← {note}" if note else ""))

        posted = 0
        for e in c.execute("SELECT type, detail FROM events WHERE key=? AND type LIKE 'comment%'",
                           (key,)):
            log(f"[{label}] {e['type']}")
            if e["type"] in ("comment_posted", "comment_dryrun"):
                posted += 1
                if e["detail"]:
                    body = (json.loads(e["detail"]).get("body") or "")
                    log("--- 게시될 뻔한 본문 ---")
                    print(body, flush=True)
                    log("--- 끝 ---")
        return posted


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shared = os.path.expanduser("~/hermes-pr/repos")
        if os.path.isdir(shared):
            for d in os.listdir(shared):
                subprocess.run(["git", "-C", os.path.join(shared, d), "worktree", "prune"],
                               capture_output=True)
    sys.exit(code)
