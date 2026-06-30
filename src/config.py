"""Config + path helpers. Single source of truth for runtime settings."""
import json
import os
import shutil

# extra dirs to search when launchd's limited PATH misses a user-installed CLI
_BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.superset/bin"),
    os.path.expanduser("~/.local/share/mise/shims"),
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
]


def subprocess_env() -> dict:
    """Env for CLI subprocesses with PATH augmented to include user bin dirs.

    Needed because launchd strips PATH, and wrappers like the Superset `claude`
    shim resolve the real binary off PATH — without ~/.local/bin they fail rc=127.
    """
    env = dict(os.environ)
    env["PATH"] = ":".join(_BIN_DIRS) + ":" + env.get("PATH", "")
    # 워크트리 체크아웃에 mise.toml(예: ceo-client, single-page)이 있으면 mise가
    # untrusted라며 codex/claude 실행을 막음(rc=1). 우리 작업 트리는 신뢰 처리.
    trusted = HERMES_HOME
    existing = env.get("MISE_TRUSTED_CONFIG_PATHS")
    env["MISE_TRUSTED_CONFIG_PATHS"] = (trusted + ":" + existing) if existing else trusted
    return env


def resolve_bin(name: str) -> str:
    """Absolute path to a CLI, robust to launchd's stripped PATH.

    Honors an absolute override in config; else PATH; else known user bin dirs.
    Falls back to the bare name so errors stay readable.
    """
    if os.path.isabs(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    for d in _BIN_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return name

HERMES_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HERMES_HOME, "config.json")


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _strip_comments(json.load(f))


def path(rel: str) -> str:
    """Resolve a path relative to HERMES_HOME (absolute paths pass through)."""
    if os.path.isabs(rel):
        return rel
    return os.path.join(HERMES_HOME, rel)


CFG = load()
