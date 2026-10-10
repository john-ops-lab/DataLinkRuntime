from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from dlr.control.services.package_source import probe_index_url


def test_live_basic_auth_probe_classifies_http_without_userinfo_dns() -> None:
    username = "synthetic-user-" + "u" * 70
    password = "synthetic:p@ss/" + "p" * 70
    expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200 if seen[-1] == expected else 401)
            self.send_header("WWW-Authenticate", 'Basic realm="test-index"')
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/simple/"
    try:
        none = probe_index_url(base)
        wrong = probe_index_url(
            base.replace(
                "//", f"//{quote(username, safe='')}:{quote('wrong-synthetic', safe='')}@", 1
            )
        )
        correct = probe_index_url(
            base.replace("//", f"//{quote(username, safe='')}:{quote(password, safe='')}@", 1)
        )
        assert none[:2] == (True, 401)
        assert wrong[:2] == (True, 401)
        assert correct == (True, 200, None)
        assert expected in seen
        for result in (none, wrong, correct):
            assert username not in str(result)
            assert password not in str(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_probe_does_not_forward_basic_auth_to_another_origin() -> None:
    seen: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers.get("Authorization") is not None
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/other/")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    servers = [source, target]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        result = probe_index_url(f"http://synthetic:secret@127.0.0.1:{source.server_port}/simple/")
        assert result == (True, 200, None)
        assert seen == [None]
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join()
