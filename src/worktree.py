"""Detached git worktrees for read-only review (ADR-009).

One blob-filtered local clone per repo (repos/<owner>__<repo>) shares its object
store across worktrees. PR head (incl. forks) is fetched via refs/pull/<n>/head.
The worktree is detached and NEVER pushed; target code is read-only.
"""
import os
import base64
import re
import subprocess
import threading

from . import config

CFG = config.CFG
GH = config.resolve_bin(CFG["gh_bin"])

# 같은 repo의 git 작업(fetch/worktree add·remove/clone)을 직렬화 — 동시 리뷰 시
# index.lock 등 충돌 방지. 다른 repo끼리는 별도 락이라 병렬 유지. (RLock = 재진입)
_repo_locks = {}
_locks_guard = threading.Lock()


def _repo_lock(repo: str) -> "threading.RLock":
    with _locks_guard:
        return _repo_locks.setdefault(repo, threading.RLock())


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def kill_review_process(repo: str, pr: int):
    """이 PR의 진행 중 리뷰(claude/codex) 프로세스를 강제 종료.
    워커는 worktree(`<slug>__pr<pr>__…`)를 --add-dir/-C 로 넘기므로 그 경로로 매칭."""
    pattern = f"{_slug(repo)}__pr{pr}__"
    subprocess.run(["pkill", "-f", pattern], capture_output=True)


def _git_env():
    env = config.subprocess_env()
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        # Launchd cannot answer GitHub credential prompts. Feed git the same
        # token gh uses, without depending on global credential-helper state.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.setdefault("GIT_CONFIG_COUNT", "1")
        env.setdefault("GIT_CONFIG_KEY_0", "http.https://github.com/.extraheader")
        env.setdefault("GIT_CONFIG_VALUE_0", f"AUTHORIZATION: basic {basic}")
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _git(repo_dir, *args, check=True, timeout=600):
    proc = subprocess.run(["git", "-C", repo_dir, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=_git_env())
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc


def ensure_clone(repo: str) -> str:
    base = config.path(CFG["repo_cache_dir"])
    os.makedirs(base, exist_ok=True)
    repo_dir = os.path.join(base, _slug(repo))
    with _repo_lock(repo):
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            # gh handles auth; blob:none keeps the clone small
            proc = subprocess.run(
                [GH, "repo", "clone", repo, repo_dir, "--", "--filter=blob:none"],
                capture_output=True, text=True, timeout=900,
                env=config.subprocess_env(),
            )
            if proc.returncode != 0:
                raise RuntimeError(f"clone {repo} failed: {proc.stderr.strip()[:300]}")
    return repo_dir


# ── 구현용 워크트리 (쓰기) ────────────────────────────────────────────────
# 리뷰용(make_worktree)과 함수를 갈라 둔다. 같은 함수에 플래그를 붙이면 리뷰 경로가
# 실수로 쓰기 가능한 워크트리를 받을 수 있고, 그건 ADR-009를 조용히 깨는 길이다.

IMPL_WT_NAME = "impl"   # repo당 상주 워크트리 1개


class ImplRepoUnknown(RuntimeError):
    """구현 대상 repo의 로컬 체크아웃 경로가 설정에 없다."""


def _impl_base_dir() -> str:
    return config.path(CFG.get("impl_workspace_dir", "workspaces"))


def impl_parent(repo: str) -> str:
    """구현 워크트리의 부모가 될 로컬 체크아웃.

    캐시 클론(repos/)은 --filter=blob:none이라 빌드·테스트가 온전히 돌지 않는다.
    사용자 체크아웃을 부모로 쓰면 오브젝트 스토어를 공유해 재클론이 없고(zigbang-client
    .git만 2.6G) partial filter도 없다."""
    raw = (CFG.get("impl_repo_paths") or {}).get(repo)
    if not raw:
        raise ImplRepoUnknown(f"{repo}: impl_repo_paths에 로컬 체크아웃 경로가 없다")
    path = os.path.expanduser(raw)
    if not os.path.exists(os.path.join(path, ".git")):
        raise ImplRepoUnknown(f"{repo}: {path} 는 git 체크아웃이 아니다")
    return path


def impl_worktree_path(repo: str) -> str:
    return os.path.join(_impl_base_dir(), _slug(repo), IMPL_WT_NAME)


def _impl_base_ref(repo_dir: str, repo: str) -> str:
    configured = (CFG.get("impl_base_ref") or {}).get(repo)
    if configured:
        return configured
    ref = _git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD", check=False).stdout.strip()
    return ref.replace("refs/remotes/", "") if ref else "origin/HEAD"


def _assert_bot_worktree(path: str):
    """reset --hard / clean 을 돌리기 전 가드.

    우리가 만든 워크스페이스 안이 아니면 절대 손대지 않는다 — 사용자 체크아웃에서
    이게 돌면 작업 중인 변경이 사라진다."""
    base = os.path.realpath(_impl_base_dir())
    real = os.path.realpath(path)
    if not real.startswith(base + os.sep):
        raise RuntimeError(f"refusing to reset a worktree outside {base}: {real}")
    if not os.path.exists(os.path.join(real, ".git")):
        raise RuntimeError(f"not a git worktree: {real}")


def run_impl_setup(repo: str, wt: str) -> bool:
    """새 워크트리에서 한 번 돌리는 설치 명령(운영자 설정)."""
    cmd = (CFG.get("impl_setup_cmd") or {}).get(repo)
    if not cmd:
        return False
    proc = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True,
                          timeout=int(CFG.get("impl_setup_timeout", 1800)),
                          env=config.subprocess_env())
    if proc.returncode != 0:
        raise RuntimeError(f"impl setup failed ({cmd}): {proc.stderr.strip()[-300:]}")
    return True


def impl_branch_name(display: str, title: str = "") -> str:
    """대상 repo의 브랜치 관례를 그대로 따른다.

    실측(zigbang/ceo-client origin/master): feature/PH-1682,
    feature/PH-1262-tax-invoice-history, feat/PH-1572/... — PH 번호가 이미 브랜치명에
    쓰인다. 봇 전용 프리픽스(lookout/*)를 쓰면 브랜치 보호 규칙이나 CI 글롭(feature/*)에서
    빠질 수 있으므로 관례를 벗어나지 않는다. 한국어 제목은 슬러그가 비므로 번호만 남는다."""
    tmpl = CFG.get("impl_branch_template") or "feature/{display}-{slug}"
    # 앞머리 [FE][CEO_APP] 같은 태그는 라우팅 메타데이터라 브랜치명에 넣지 않는다
    body = re.sub(r"^(\s*\[[^\]]*\])+", "", title or "")
    slug = re.sub(r"[^a-z0-9]+", "-", body.lower()).strip("-")[:40].strip("-")
    return re.sub(r"[-/]+$", "", tmpl.format(display=display, slug=slug))


def _branch_exists(repo_dir: str, branch: str) -> bool:
    return _git(repo_dir, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}", check=False).returncode == 0


def make_impl_worktree(repo: str, branch: str, base_ref: str = None,
                       setup: bool = True) -> str:
    """repo당 상주 구현 워크트리를 준비하고 `branch`로 세운다.

    이슈마다 새 워크트리를 파면 node_modules를 매번 새로 설치해야 한다(zigbang-client는
    체크아웃 21G 중 대부분이 그것). 그래서 repo당 하나를 두고 브랜치만 갈아 쓴다.
    동시 작업은 _repo_lock으로 직렬화된다.

    **이미 있는 브랜치는 base로 되감지 않는다.** 워크트리를 공유하므로 같은 카드가
    검증·재구현으로 이 함수를 다시 부르고, 그때 -B로 브랜치를 다시 만들면 앞선
    커밋이 조용히 사라진다."""
    parent = impl_parent(repo)
    wt = impl_worktree_path(repo)
    fresh = False
    with _repo_lock(repo):
        base = base_ref or _impl_base_ref(parent, repo)
        _git(parent, "fetch", "--quiet", "origin")
        exists = _branch_exists(parent, branch)
        if os.path.exists(os.path.join(wt, ".git")):
            _assert_bot_worktree(wt)
            # 지난 작업 잔여물 정리. -x 는 쓰지 않는다 — node_modules(ignored)를
            # 지워버리면 상주 워크트리를 두는 이유가 없어진다.
            _git(wt, "reset", "--hard", check=False)
            _git(wt, "clean", "-fd", check=False)
            if exists:
                _git(wt, "checkout", branch)
            else:
                _git(wt, "checkout", "-b", branch, base)
        else:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            _git(parent, "worktree", "prune")
            if exists:
                _git(parent, "worktree", "add", wt, branch)
            else:
                _git(parent, "worktree", "add", "-b", branch, wt, base)
            fresh = True
    if fresh and setup:
        run_impl_setup(repo, wt)
    return wt


def remove_impl_worktree(repo: str):
    """워크트리만 지운다. 브랜치는 남긴다 — PR이 그 브랜치를 가리키고 있을 수 있다."""
    wt = impl_worktree_path(repo)
    if not os.path.exists(os.path.join(wt, ".git")):
        return
    _assert_bot_worktree(wt)
    parent = impl_parent(repo)
    with _repo_lock(repo):
        _git(parent, "worktree", "remove", "--force", wt, check=False)
        _git(parent, "worktree", "prune", check=False)


def make_worktree(repo: str, pr: int, head_sha: str) -> str:
    repo_dir = ensure_clone(repo)
    wt_base = config.path(CFG["worktree_dir"])
    os.makedirs(wt_base, exist_ok=True)
    wt = os.path.join(wt_base, f"{_slug(repo)}__pr{pr}__{head_sha[:10]}")
    # 같은 repo의 fetch/worktree add는 직렬화 (다른 repo는 병렬)
    with _repo_lock(repo):
        _git(repo_dir, "fetch", "--quiet", "origin", f"pull/{pr}/head")
        if os.path.isdir(wt):
            _git(repo_dir, "worktree", "remove", "--force", wt, check=False)
        _git(repo_dir, "worktree", "add", "--detach", "--force", wt, head_sha)
    # mise.toml이 있으면 codex/claude 실행 시 mise가 'untrusted'로 막음(rc=1).
    # 일회용 detached 트리라 제거해도 무해(diff는 프롬프트에 그대로 있음).
    for f in ("mise.toml", ".mise.toml", "mise/config.toml", ".config/mise/config.toml"):
        p = os.path.join(wt, f)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return wt


def local_diff(repo: str, pr: int, head_sha: str, base_ref: str) -> str:
    """Three-dot diff computed in the cached clone — the fallback for PRs whose
    diff GitHub's API refuses (>20k lines).

    Same semantics as the PR page: merge-base(base tip, head)..head. The base tip
    is fetched into a throwaway ref (deleted after) so nothing pins objects; if the
    base branch is gone (renamed/deleted after the PR opened) we fall back to the
    clone's default branch, which is wrong-but-readable rather than a hard failure.
    """
    repo_dir = ensure_clone(repo)
    tmp_ref = f"refs/lookout/base/pr{pr}"
    with _repo_lock(repo):
        _git(repo_dir, "fetch", "--quiet", "origin", f"pull/{pr}/head")
        fetched = _git(repo_dir, "fetch", "--quiet", "origin",
                       f"+{base_ref}:{tmp_ref}", check=False)
        base = tmp_ref
        if fetched.returncode != 0:
            _git(repo_dir, "remote", "set-head", "origin", "--auto", check=False)
            base = "origin/HEAD"
        try:
            mb = _git(repo_dir, "merge-base", base, head_sha).stdout.strip()
            return _git(repo_dir, "diff", mb, head_sha, timeout=900).stdout
        finally:
            _git(repo_dir, "update-ref", "-d", tmp_ref, check=False)


def remove_worktree(repo: str, wt: str):
    repo_dir = os.path.join(config.path(CFG["repo_cache_dir"]), _slug(repo))
    with _repo_lock(repo):
        _git(repo_dir, "worktree", "remove", "--force", wt, check=False)


def gc_worktrees():
    """Prune stale worktree registrations across all cached repos."""
    base = config.path(CFG["repo_cache_dir"])
    if not os.path.isdir(base):
        return
    for slug in os.listdir(base):
        rd = os.path.join(base, slug)
        if os.path.isdir(os.path.join(rd, ".git")):
            _git(rd, "worktree", "prune", check=False)


def gc_repos():
    """캐시 repo의 누적 객체 회수 (제거된 워크트리의 옛 PR-head 객체 등).

    안전성: ① per-repo 락으로 진행 중 fetch/worktree-add와 직렬화,
    ② git gc는 모든 워크트리 HEAD를 루트로 보존하므로 진행 중 리뷰가 체크아웃한
    head_sha 객체는 prune 대상이 아님. worktree prune 먼저 → 제거된 트리 등록을
    정리해 그 객체가 회수 가능해진 뒤 gc."""
    base = config.path(CFG["repo_cache_dir"])
    if not os.path.isdir(base):
        return
    for slug in os.listdir(base):
        rd = os.path.join(base, slug)
        if not os.path.isdir(os.path.join(rd, ".git")):
            continue
        repo = slug.replace("__", "/")  # make_worktree와 동일한 락 키
        with _repo_lock(repo):
            _git(rd, "worktree", "prune", check=False)
            # 진행 중 리뷰의 워크트리가 남아있으면 이번 주기는 건너뜀(다음에 회수).
            # 같은 프로세스 밖의 리뷰 fetch와도 충돌하지 않도록 하는 안전장치.
            wl = _git(rd, "worktree", "list", "--porcelain", check=False)
            if wl.stdout.count("worktree ") > 1:
                continue
            _git(rd, "gc", "--prune=now", "--quiet", check=False, timeout=1200)
