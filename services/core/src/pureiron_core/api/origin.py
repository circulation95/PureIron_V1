"""브라우저 요청의 Origin 검사(plan M3c D14): WS 업그레이드(`api/ws.py`)와 REST 비안전 메서드가 같은
host:port 비교를 쓴다.

REST 규칙(`OriginGuard`): 세션 쿠키(`pureiron_session`)가 실린 POST·PUT·PATCH·DELETE는
`Origin`이 있어야 하고 그 host:port가 요청 `Host`와 같아야 하며, `Sec-Fetch-Site`가 있으면
`same-origin`이어야 한다. 로그인 경로는 쿠키가 없어도 `Origin`이 있으면 같은 검사를 한다
(login CSRF — 다른 사이트가 공격자 계정으로 로그인시키는 것).
쿠키 없이 Bearer만 보내는 비브라우저 클라이언트(ai·Updater·스크립트)는 검사하지 않는다 —
브라우저는 다른 origin 요청에 Authorization 헤더를 스스로 싣지 않는다.
같은 PC의 다른 포트 웹앱은 same-site라 SameSite=Strict 쿠키가 실린다 — 이 검사가 그 경로를 막는다.
실패는 403 `{"detail": "E_ORIGIN"}`, 감사 없이 경고 로그만(인증 전 요청이 감사를 채우지 않게).
프록시는 브라우저의 Host를 그대로 넘겨야 한다(nginx `$http_host`, vite `changeOrigin` 끔).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import status
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from pureiron_core.api.auth import SESSION_COOKIE

log = logging.getLogger(__name__)

ORIGIN_REJECTED = "E_ORIGIN"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def same_origin(origin: str, host: str, *, secure: bool) -> bool:
    """Origin의 host:port가 요청 Host와 같은가. 포트가 없으면 각자 scheme의 기본 포트다(Origin은
    http/https, Host는 이 요청의 scheme — `secure`면 443)."""
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False  # "null"(sandbox·file) 등
    requested = urlsplit("//" + host)
    try:
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host_port = requested.port or (443 if secure else 80)
    except ValueError:
        return False
    return (parsed.hostname, origin_port) == (requested.hostname, host_port)


def _rejection(request: Request, login_path: str) -> str | None:
    """거부 이유(로그용), 통과면 None."""
    origin = request.headers.get("origin")
    login_with_origin = request.url.path == login_path and origin is not None
    if SESSION_COOKIE not in request.cookies and not login_with_origin:
        return None
    if origin is None:
        return "missing Origin"
    host = request.headers.get("host", "")
    if not same_origin(origin, host, secure=request.url.scheme == "https"):
        return f"Origin {origin!r} does not match Host {host!r}"
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        return f"Sec-Fetch-Site {fetch_site!r}"
    return None


class OriginGuard:
    """REST Origin 검사 ASGI 미들웨어. `app.add_middleware(OriginGuard, login_path=...)`로 건다."""

    def __init__(self, app: ASGIApp, *, login_path: str) -> None:
        self._app = app
        self._login_path = login_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            request = Request(scope)
            reason = _rejection(request, self._login_path)
            if reason is not None:
                log.warning("rejected %s %s: %s", request.method, request.url.path, reason)
                rejected = JSONResponse(
                    {"detail": ORIGIN_REJECTED}, status_code=status.HTTP_403_FORBIDDEN
                )
                await rejected(scope, receive, send)
                return
        await self._app(scope, receive, send)
