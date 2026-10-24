"""REST Origin 검사(plan M3c D14): 안전 메서드가 아닌 요청은 쿠키가 있으면 Origin 필수, Origin이
있으면(쿠키·경로 무관) Origin = Host이고 Sec-Fetch-Site가 없거나 same-origin일 때만 통과,
쿠키도 Origin도 없는 요청(Bearer)·안전 메서드는 통과, 거부는 403 `E_ORIGIN` + 경고 로그."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from pureiron_core.api.auth import SESSION_COOKIE
from pureiron_core.api.origin import OriginGuard, same_origin

PROBE = "/api/v1/probe"
LOGIN = "/api/v1/auth/login"
SAME = "http://testserver"  # TestClient의 Host는 `testserver`
REJECTED = {"detail": "E_ORIGIN"}


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(OriginGuard)
    router = APIRouter()

    @router.api_route(
        "/probe", methods=["GET", "HEAD", "OPTIONS", "TRACE", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router, prefix="/api/v1")
    return app


def _with_cookie() -> TestClient:
    return TestClient(_app(), cookies={SESSION_COOKIE: "any"})


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "PROPFIND"])
def test_cookie_without_origin_is_403(method: str) -> None:
    """안전 메서드가 아니면 전부 대상이다(라우트가 없는 메서드도 — deny-by-default)."""
    response = _with_cookie().request(method, PROBE)
    assert (response.status_code, response.json()) == (403, REJECTED)


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver:8080",  # 같은 PC의 다른 포트 = same-site, Strict 쿠키가 실린다
        "http://evil.example",
        "https://testserver",  # 443 ≠ 80
        "null",
    ],
)
def test_cookie_with_other_origin_is_403(origin: str) -> None:
    response = _with_cookie().post(PROBE, headers={"Origin": origin})
    assert (response.status_code, response.json()) == (403, REJECTED)


def test_cookie_with_same_origin_passes() -> None:
    client = _with_cookie()
    assert client.post(PROBE, headers={"Origin": SAME}).status_code == 200
    assert client.post(PROBE, headers={"Origin": "http://testserver:80"}).status_code == 200


@pytest.mark.parametrize(
    ("site", "status"),
    [("same-origin", 200), ("same-site", 403), ("none", 403), ("cross-site", 403)],
)
def test_sec_fetch_site_must_be_same_origin_when_present(site: str, status: int) -> None:
    response = _with_cookie().post(PROBE, headers={"Origin": SAME, "Sec-Fetch-Site": site})
    assert response.status_code == status


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "TRACE"])
def test_safe_methods_are_not_checked(method: str) -> None:
    response = _with_cookie().request(method, PROBE, headers={"Origin": "http://evil.example"})
    assert response.status_code == 200


def test_bearer_only_requests_are_not_checked() -> None:
    """비브라우저(ai·Updater·스크립트)는 쿠키 없이 Bearer만 보낸다 — Origin이 없어도 통과."""
    client = TestClient(_app())
    assert client.post(PROBE, headers={"Authorization": "Bearer t"}).status_code == 200
    assert client.delete(PROBE).status_code == 200


def test_session_cookie_among_other_cookies_is_detected() -> None:
    client = TestClient(_app())
    raw = client.post(PROBE, headers={"Cookie": "a=b; pureiron_session=x"})
    assert (raw.status_code, raw.json()) == (403, REJECTED)
    assert client.post(PROBE, headers={"Cookie": "other=x"}).status_code == 200  # 세션 쿠키 아님


def test_cookie_with_bearer_still_needs_origin() -> None:
    """쿠키와 Bearer가 둘 다 오면 가드는 쿠키를 쓴다 — Bearer가 있다고 검사를 건너뛰지 않는다."""
    response = _with_cookie().post(PROBE, headers={"Authorization": "Bearer t"})
    assert (response.status_code, response.json()) == (403, REJECTED)


@pytest.mark.parametrize("path", [LOGIN, LOGIN + "/", PROBE])
def test_origin_without_cookie_is_checked_on_any_path(path: str) -> None:
    """login CSRF: 다른 사이트가 공격자 계정으로 로그인시키는 것을 막는다. 경로 비교에 기대지
    않도록(끝 슬래시 등) Origin이 실린 비안전 요청은 경로와 무관하게 검사한다."""
    client = TestClient(_app())
    cross = client.post(path, headers={"Origin": "http://testserver:3000"})
    assert (cross.status_code, cross.json()) == (403, REJECTED)
    site = client.post(path, headers={"Origin": SAME, "Sec-Fetch-Site": "same-site"})
    assert site.status_code == 403


def test_login_without_origin_or_with_same_origin_passes() -> None:
    client = TestClient(_app())
    assert client.post(LOGIN).status_code == 200  # Origin 없음 = 비브라우저
    assert client.post(LOGIN, headers={"Origin": SAME}).status_code == 200
    assert _with_cookie().post(LOGIN).status_code == 403  # 쿠키가 실렸으면 Origin 필수


def test_rejection_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pureiron_core.api.origin"):
        _with_cookie().post(PROBE, headers={"Origin": "http://evil.example"})
    assert any(
        "rejected POST '/api/v1/probe'" in r.getMessage() and "evil.example" in r.getMessage()
        for r in caplog.records
    )


async def test_rejection_log_escapes_decoded_path(caplog: pytest.LogCaptureFixture) -> None:
    """path는 서버가 디코드한 값이다 — 제어 문자(터미널 escape 등)로 로그를 위조하지 못하게
    이스케이프한다(개행·탭은 URL 파싱이 이미 지운다). httpx는 제어 문자를 지워 보내므로 ASGI
    scope를 직접 만든다."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/x\x1b[2Jforged",
        "raw_path": b"/api/v1/x%1B[2Jforged",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"cookie", f"{SESSION_COOKIE}=x".encode())],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    with caplog.at_level(logging.WARNING, logger="pureiron_core.api.origin"):
        await OriginGuard(_app())(scope, receive, send)
    assert sent[0]["status"] == 403
    messages = [r.getMessage() for r in caplog.records]
    assert any("'/api/v1/x\\x1b[2Jforged'" in m for m in messages)
    assert not any("\x1b" in m for m in messages)


@pytest.mark.parametrize(
    ("origin", "host", "secure", "same"),
    [
        ("http://site:8080", "site:8080", False, True),
        ("http://site", "site:80", False, True),
        ("https://site", "site", True, True),
        ("https://site", "site", False, False),  # 443 ≠ http 기본 80
        ("http://site:3000", "site:8080", False, False),
        ("http://other:8080", "site:8080", False, False),
        ("null", "site", False, False),
        ("ftp://site", "site", False, False),
        ("http://site:99999", "site", False, False),  # 포트 범위 밖
        ("http://site:80", "site", False, True),
        ("http://SITE:8080", "site:8080", False, True),  # host 대소문자 무시
        ("http://[::1]:8080", "[::1]:8080", False, True),  # IPv6
        ("http://[::1]:8080", "[::1]:3000", False, False),
        ("http://site", "", False, False),  # Host 누락 = 거부
    ],
)
def test_same_origin_compares_host_and_port(
    origin: str, host: str, secure: bool, same: bool
) -> None:
    assert same_origin(origin, host, secure=secure) is same
