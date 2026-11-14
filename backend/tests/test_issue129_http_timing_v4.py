"""Linux IO timing excludes local startup while retaining the outer deadline."""

import os
import signal
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from dlr.worker import claim_http_v4


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal"),
    "requires Linux supervised HTTP IO",
)
class RequestTimingTests(unittest.TestCase):
    def test_real_io_interval_excludes_spawn_and_expired_deadline_sends_nothing(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(True)
                time.sleep(0.12)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_spawn = claim_http_v4._spawn_until

        def delayed_spawn(*args, **kwargs):
            time.sleep(0.25)
            return original_spawn(*args, **kwargs)

        kwargs = {
            "url": f"http://127.0.0.1:{server.server_port}/proof",
            "credential": "test-only",
            "instance": "test-only",
            "generation": None,
            "body": "{}",
            "timeout_seconds": 3.0,
            "_proof_request": True,
            "_collect_request_timing": True,
            "_test_only_plaintext_loopback": True,
            "_response_limit_bytes": 8192,
        }
        try:
            started = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            with patch.object(claim_http_v4, "_spawn_until", delayed_spawn):
                result = claim_http_v4.request_claim(**kwargs)
            finished = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            self.assertEqual((result.state, result.status, result.body), ("response", 200, b"{}"))
            self.assertIsNotNone(result.request_interval_ns)
            sent, received = result.request_interval_ns
            self.assertLessEqual(started, sent)
            self.assertLessEqual(received, finished)
            self.assertGreaterEqual(received - sent, 100_000_000)
            self.assertGreaterEqual((finished - started) - (received - sent), 240_000_000)
            self.assertFalse(claim_http_v4._unresolved)
            expired = claim_http_v4.request_claim(
                **kwargs, _absolute_deadline=claim_http_v4._clock() - 0.1
            )
            self.assertNotEqual(expired.state, "response")
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
