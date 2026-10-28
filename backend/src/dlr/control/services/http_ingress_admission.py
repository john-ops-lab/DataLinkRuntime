"""Cheap refusal of known-full HTTP body/native-work budgets before middleware.

This is only a read-only hint for the existing gate. It neither receives a
body nor creates resources, and admitted requests retain their original path.
"""

from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

_BUSY_BODY = (
    b'{"detail":{"code":"ingress_capacity_exceeded","message":"Reception resources are busy"}}'
)
_BUSY_HEADERS = [
    (b"content-length", str(len(_BUSY_BODY)).encode("ascii")),
    (b"content-type", b"application/json"),
    (b"retry-after", b"1"),
]


def _is_ingress(path: str) -> bool:
    prefix = "/__dlr_account"
    if path.startswith(prefix + "/"):
        path = path[len(prefix) :]
    if not path.startswith("/api/ingress/"):
        return False
    public_id = path[len("/api/ingress/") :]
    return bool(public_id) and "/" not in public_id


class HttpIngressAdmission:
    def __init__(
        self,
        app: ASGIApp,
        *,
        busy: Callable[[], bool],
        busy_route: Callable[[str], bool] | None = None,
    ) -> None:
        self.app = app
        self.busy = busy
        self.busy_route = busy_route

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and _is_ingress(scope["path"])
            and (
                (
                    self.busy_route is not None and self.busy_route(scope["path"].rsplit("/", 1)[1])
                )
                or self.busy()
            )
        ):
            await send({"type": "http.response.start", "status": 429, "headers": _BUSY_HEADERS})
            await send({"type": "http.response.body", "body": _BUSY_BODY})
            return
        await self.app(scope, receive, send)
