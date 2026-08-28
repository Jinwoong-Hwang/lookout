"""macOS 데스크톱 알림. 알림 실패가 tick을 죽이면 안 되므로 전부 삼킨다.

terminal-notifier가 있으면 그걸 쓰고(-group으로 같은 종류 알림을 덮어씀), 없으면
osascript로 떨어뜨린다. launchd는 PATH를 비우므로 config.subprocess_env()를 쓴다.
"""
import os
import subprocess

from . import config

CFG = config.CFG


def _q(s: str) -> str:
    """AppleScript 문자열 리터럴 이스케이프."""
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(title: str, message: str, subtitle: str = "", group: str = "lookout") -> bool:
    """알림 1건. 성공하면 True. 어떤 예외도 밖으로 내보내지 않는다."""
    if not CFG.get("notify_enabled", True):
        return False
    env = config.subprocess_env()
    try:
        tn = config.resolve_bin("terminal-notifier")
        if os.path.isabs(tn):
            args = [tn, "-title", title, "-message", message, "-group", group]
            if subtitle:
                args += ["-subtitle", subtitle]
            return subprocess.run(args, capture_output=True, timeout=10,
                                  env=env).returncode == 0
        script = f"display notification {_q(message)} with title {_q(title)}"
        if subtitle:
            script += f" subtitle {_q(subtitle)}"
        return subprocess.run(["osascript", "-e", script], capture_output=True,
                              timeout=10, env=env).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
