"""Webhook receiver (ADR-001 intake, LLM-free).

GitHub -> Hookdeck -> (hookdeck CLI forward) -> this localhost server.
Responsibilities ONLY: verify HMAC, dedupe by delivery id, persist raw event,
return 202 immediately. The router (run by tick.py) turns events into cards.

Run: python -m src.receiver
"""
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db

CFG = config.CFG
SECRET = CFG["webhook_secret"].encode()
PATH = CFG["receiver_path"]
SLACK_PATH = CFG.get("slack_path", "/slack")
SLACK_SECRET = CFG.get("slack_signing_secret", "").encode()


def _valid_signature(body: bytes, sig_header: str) -> bool:
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _valid_slack(body: bytes, ts: str, sig: str) -> bool:
    """Slack v0 signing. Hookdeck's Slack source already verifies at the edge;
    this is defense-in-depth. Timestamp anti-replay is skipped on purpose —
    Hookdeck queue/retry can legitimately delay delivery past Slack's 5-min window."""
    if not SLACK_SECRET or not ts or not sig:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(SLACK_SECRET, base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet default logging
        pass

    def _reply(self, code: int, msg: str):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": msg}).encode())

    def _reply_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        path = self.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if path == SLACK_PATH.rstrip("/"):
            self._handle_slack(body)
            return
        if path != PATH.rstrip("/"):
            self._reply(404, "not found")
            return
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not _valid_signature(body, sig):
            self._reply(401, "bad signature")
            return
        event_type = self.headers.get("X-GitHub-Event", "unknown")
        delivery = self.headers.get("X-GitHub-Delivery", "")
        if event_type == "ping":
            self._reply(200, "pong")
            return
        with db.connect() as c:
            fresh = db.enqueue_inbox(c, delivery or f"nodelivery:{hash(body)}", event_type, body.decode("utf-8", "replace"))
            db.log_event(c, "webhook_received", detail={"event": event_type, "delivery": delivery, "fresh": fresh})
        self._reply(202, "accepted")

    def _handle_slack(self, body: bytes):
        try:
            data = json.loads(body or b"{}")
        except Exception:  # noqa: BLE001
            self._reply(400, "bad json")
            return
        # One-time Events API URL verification handshake.
        if data.get("type") == "url_verification":
            self._reply_text(200, data.get("challenge", ""))
            return
        sig = self.headers.get("X-Slack-Signature", "")
        ts = self.headers.get("X-Slack-Request-Timestamp", "")
        if sig and not _valid_slack(body, ts, sig):  # verify only if header present
            self._reply(401, "bad signature")
            return
        ev = data.get("event", {}) or {}
        event_id = data.get("event_id") or f"{ev.get('channel', '')}:{ev.get('ts', '')}"
        with db.connect() as c:
            fresh = db.enqueue_inbox(c, f"slack:{event_id}", "slack", body.decode("utf-8", "replace"))
            db.log_event(c, "slack_received", detail={"event_id": event_id, "fresh": fresh, "verified": bool(sig)})
        self._reply(202, "accepted")


def main():
    db.init()
    addr = (CFG["receiver_host"], CFG["receiver_port"])
    server = ThreadingHTTPServer(addr, Handler)
    print(f"[receiver] listening on http://{addr[0]}:{addr[1]}{PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
