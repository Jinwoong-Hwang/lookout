"""Resolve Slack user/channel ids to display names, cached in `slack_names`.

Needs `slack_bot_token` + `users:read` / `channels:read` / `groups:read` scopes.
Network/API failure degrades to the raw id and is NOT cached (so it retries
later) — intake (ADR-001) is never blocked by a Slack API hiccup.
"""
import json
import urllib.parse
import urllib.request

from . import config, db

CFG = config.CFG
TOKEN = CFG.get("slack_bot_token", "")


def _api(method: str, **params):
    url = "https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve(c, sid: str, kind: str) -> str:
    if not sid:
        return ""
    cached = db.get_slack_name(c, sid)
    if cached is not None:
        return cached
    resolved = None
    if TOKEN:
        try:
            if kind == "user":
                d = _api("users.info", user=sid)
                if d.get("ok"):
                    u = d["user"]
                    resolved = (u.get("profile", {}).get("display_name")
                                or u.get("real_name") or u.get("name"))
            else:
                d = _api("conversations.info", channel=sid)
                if d.get("ok"):
                    resolved = "#" + d["channel"].get("name", sid)
        except Exception:  # noqa: BLE001 - degrade to id, never block intake
            resolved = None
    if resolved:
        db.set_slack_name(c, sid, kind, resolved)
        return resolved
    return sid  # fallback (uncached → retried next time token/scopes are fixed)


def user(c, uid: str) -> str:
    return _resolve(c, uid, "user")


def channel(c, cid: str) -> str:
    return _resolve(c, cid, "channel")
