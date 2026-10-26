"""Serve the app under an optional path prefix (``PUBLIC_BASE_PATH``).

Cloudflare Tunnel path routing forwards ``/livekit/call/x`` unchanged and cannot
rewrite it, so the prefix is handled here: requests under it get the prefix set
as ASGI ``root_path`` (Starlette routing then matches on the remainder), and
``url_for_path`` re-applies it when building redirect URLs.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


class BasePathMiddleware:
    def __init__(self, app: ASGIApp, base_path: str) -> None:
        self.app = app
        self.base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.base_path and scope["type"] in ("http", "websocket"):
            path: str = scope["path"]
            root_path: str = scope.get("root_path", "")
            under_prefix = path == self.base_path or path.startswith(self.base_path + "/")
            # Idempotent, so wrapping an already prefix-aware app is harmless.
            if under_prefix and not root_path.endswith(self.base_path):
                scope = {**scope, "root_path": scope.get("root_path", "") + self.base_path}
        await self.app(scope, receive, send)


def url_for_path(request: Request, path: str) -> str:
    """App-absolute ``path`` -> URL path including any prefix this request used."""
    root_path: str = request.scope.get("root_path", "")
    return root_path + path
