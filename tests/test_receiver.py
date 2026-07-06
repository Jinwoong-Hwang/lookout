import io
import unittest

from src import receiver


class FakeHandler:
    path = "/"
    _reply = receiver.Handler._reply

    def __init__(self, host="127.0.0.1:8787"):
        self.headers = {"Host": host}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


class ReceiverGetTest(unittest.TestCase):
    def test_root_redirects_to_dashboard_on_same_host(self):
        h = FakeHandler("192.168.0.57:8787")
        receiver.Handler.do_GET(h)
        self.assertEqual(h.status, 302)
        self.assertEqual(h.sent_headers["Location"], "//192.168.0.57:8788/")

    def test_health_stays_json(self):
        h = FakeHandler()
        h.path = "/health"
        receiver.Handler.do_GET(h)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.wfile.getvalue(), b'{"status": "ok"}')


if __name__ == "__main__":
    unittest.main()
