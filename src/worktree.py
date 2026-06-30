"""Detached git worktrees for read-only review (ADR-009).

One blob-filtered local clone per repo (repos/<owner>__<repo>) shares its object
store across worktrees. PR head (incl. forks) is fetched via refs/pull/<n>/head.
The worktree is detached and NEVER pushed; target code is read-only.
"""
import os
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


def _git(repo_dir, *args, check=True, timeout=600):
    proc = subprocess.run(["git", "-C", repo_dir, *args],
                          capture_output=True, text=True, timeout=timeout)
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
