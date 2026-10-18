"""브라우저 요청의 Origin 검사(plan M3c D14): WS 업그레이드(`api/ws.py`)와 REST 비안전 메서드가 같은
host:port 비교를 쓴다.

REST 규칙(`OriginGuard`): 안전 메서드(GET·HEAD·OPTIONS·TRACE)가 아닌 요청은 전부 대상이다.
- 세션 쿠키(`pureiron_session`)가 실렸으면 `Origin`이 있어야 한다.
- `Origin`이 있으면(쿠키가 없어도, 경로와 무관하게 — login CSRF 포함) 그 host:port가 요청
  `Host`와 같아야 하고, `Sec-Fetch-Site`가 있으면 `same-origin`이어야 한다.
- 쿠키도 `Origin`도 없는 요청(Bearer를 쓰는 ai·Updater·스크립트)은 검사하지 않는다 — 브라우저는
  비안전 메서드에 `Origin`을 늘 싣고, 다른 origin 요청에 Authorization 헤더를 스스로 싣지 않는다.
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
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


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


def _rejection(request: Request) -> str | None:
    """거부 이유(로그용), 통과면 None."""
    origin = request.headers.get("origin")
    if origin is None:
        return "missing Origin" if SESSION_COOKIE in request.cookies else None
    host = request.headers.get("host", "")
    if not same_origin(origin, host, secure=request.url.scheme == "https"):
        return f"Origin {origin!r} does not match Host {host!r}"
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        return f"Sec-Fetch-Site {fetch_site!r}"
    return None


class OriginGuard:
    """REST Origin 검사 ASGI 미들웨어. `app.add_middleware(OriginGuard)`로 건다."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            request = Request(scope)
            reason = _rejection(request)
            if reason is not None:
                # path는 디코드된 값이라 %r로 찍는다(제어 문자로 로그를 위조하지 못하게).
                log.warning("rejected %s %r: %s", request.method, request.url.path, reason)
                rejected = JSONResponse(
                    {"detail": ORIGIN_REJECTED}, status_code=status.HTTP_403_FORBIDDEN
                )
                await rejected(scope, receive, send)
                return
        await self._app(scope, receive, send)
