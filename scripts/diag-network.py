#!/usr/bin/env python3
"""Layered network / DNS diagnostics for DLR container deployments (M5.5.3).

Runs the four failure layers in order and stops at the first failure so the
operator can tell exactly which layer is broken:

    1. DNS resolution     (hostname -> IP)
    2. TCP connect        (host:port reachable)
    3. TLS handshake      (only for https URLs)
    4. HTTP request       (optional URL path)

Exit codes: 0 = all layers passed, 2 = DNS, 3 = TCP, 4 = TLS, 5 = HTTP,
1 = usage/argument error. No tokens, credentials or secrets are ever read,
sent or printed; only the configured URL and its hostname/port are used.

Examples:
    # host-based check: DNS + TCP only (--tls adds the TLS handshake layer)
    python3 scripts/diag-network.py --host api.deepseek.com --port 443
    python3 scripts/diag-network.py --host api.deepseek.com --port 8443 --tls

    # URL-based check with a real HTTP probe
    python3 scripts/diag-network.py --url https://api.deepseek.com/v1/models

    # run inside the control container (same DNS as the Control Node):
    docker compose exec -T control python - < scripts/diag-network.py \
        --url https://api.deepseek.com

Uses only the Python standard library so it works on the host, inside any
DLR image, and in the Compose smoke network.
"""

import argparse
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import NoReturn
from urllib.parse import urlsplit

DNS_EXIT = 2
TCP_EXIT = 3
TLS_EXIT = 4
HTTP_EXIT = 5

_CONNECT_TIMEOUT_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1024 * 1024


def _fail(exit_code: int, layer: str, detail: str) -> NoReturn:
    print(f"[FAIL] {layer}: {detail}", file=sys.stderr)
    print(
        f"hint: {layer} 失败，请对照 README「容器网络与 DNS 排障」检查对应层级；"
        "默认 Compose 使用 Docker 内置 DNS（转发宿主机 resolv.conf），"
        "企业网络 / VPN 下可参考 docker-compose.dns.example.yml 覆盖 DNS。",
        file=sys.stderr,
    )
    raise SystemExit(exit_code)


def check_dns(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as error:
        _fail(DNS_EXIT, "DNS 解析", f"无法解析域名 {host!r}（{error}）")
    addresses = sorted({info[4][0] for info in infos})
    print(f"[ OK ] DNS 解析: {host} -> {', '.join(addresses)}")
    return addresses


def check_tcp(host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            pass
    except (socket.timeout, TimeoutError) as error:
        _fail(TCP_EXIT, "TCP 连接", f"{host}:{port} 连接超时（{error}）")
    except OSError as error:
        _fail(TCP_EXIT, "TCP 连接", f"{host}:{port} 连接失败（{error}）")
    print(f"[ OK ] TCP 连接: {host}:{port} 可达")


def check_tls(host: str, port: int) -> None:
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                version = tls.version()
    except ssl.SSLError as error:
        _fail(TLS_EXIT, "TLS 握手", f"{host}:{port} TLS 握手失败（{error}）")
    except OSError as error:
        _fail(TLS_EXIT, "TLS 握手", f"{host}:{port} TLS 阶段连接失败（{error}）")
    print(f"[ OK ] TLS 握手: {host}:{port}（{version}）")


def check_http(url: str) -> None:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            # Read a bounded amount so a streaming/gateway endpoint cannot hang.
            response.read(_MAX_RESPONSE_BYTES + 1)
            print(f"[ OK ] HTTP 请求: {url} -> {response.status} {response.reason}")
    except urllib.error.HTTPError as error:
        # A 4xx/5xx means the HTTP layer works; TLS and network are fine.
        print(f"[ OK ] HTTP 请求: {url} -> {error.code}（应用层已可达，非网络问题）")
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as error:
        _fail(HTTP_EXIT, "HTTP 请求", f"{url} 请求失败（{error}）")


def _parse_url(url: str) -> tuple[str, str, int, str]:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        _fail(1, "参数", f"无法解析 URL: {url!r}")
    if parts.scheme not in ("http", "https") or host is None:
        _fail(1, "参数", f"URL 必须是 http(s) 绝对地址: {url!r}")
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, host, port, url


class _ArgumentParser(argparse.ArgumentParser):
    """Exit code 1 on usage/argument errors.

    ``argparse`` defaults to exit code 2, which would collide with this
    script's DNS-layer exit code. Keeping 1 = usage/argument error lets
    operators write layer-based exit-code checks without misreading a
    mistyped command line as a DNS failure.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv: list[str]) -> None:
    parser = _ArgumentParser(
        description="DLR 分层网络 / DNS 诊断：DNS -> TCP -> TLS -> HTTP",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--host",
        help="仅检查到主机的 DNS 与 TCP（可加 --port 与 --tls；不做 HTTP 探测）",
    )
    group.add_argument("--url", help="检查完整 URL：DNS/TCP 与 https 时含 TLS/HTTP")
    parser.add_argument(
        "--port",
        type=int,
        help="TCP 端口（与 --host 配合，默认 443；仅用于 DNS/TCP/TLS 层）",
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        help="与 --host 配合：在 DNS/TCP 之外额外执行 TLS 握手检查",
    )
    args = parser.parse_args(argv)

    if args.url is not None:
        if args.tls:
            parser.error("--tls 仅与 --host 配合使用，--url 模式按 URL 的 scheme 自动决定 TLS")
        if args.port is not None:
            parser.error("--port 仅与 --host 配合使用，--url 的端口取自 URL")
        scheme, host, port, url = _parse_url(args.url)
        check_dns(host, port)
        check_tcp(host, port)
        if scheme == "https":
            check_tls(host, port)
        check_http(url)
        print("[ OK ] 全部网络层级检查通过")
        return

    # --host mode is deliberately limited to DNS/TCP (+ optional TLS): probing
    # an arbitrary port with an HTTP request would mislead the operator into
    # the wrong layer for non-HTTP services (e.g. a database port).
    host = args.host
    port = args.port if args.port is not None else 443
    check_dns(host, port)
    check_tcp(host, port)
    if args.tls:
        check_tls(host, port)
    print("[ OK ] DNS 与 TCP 检查通过" + ("（含 TLS 握手）" if args.tls else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
