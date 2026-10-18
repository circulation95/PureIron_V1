"""REST Origin 검사(plan M3c D14): 쿠키가 실린 비안전 메서드는 Origin = Host이고
Sec-Fetch-Site가 없거나 same-origin일 때만 통과, login은 Origin이 있으면 같은 검사,
Bearer만 있는 요청·안전 메서드는 통과, 거부는 403 `E_ORIGIN` + 경고 로그."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.auth import SESSION_COOKIE
from pureiron_core.api.origin import OriginGuard, same_origin

PROBE = "/api/v1/probe"
LOGIN = "/api/v1/auth/login"
SAME = "http://testserver"  # TestClient의 Host는 `testserver`
REJECTED = {"detail": "E_ORIGIN"}


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(OriginGuard, login_path=LOGIN)
    router = APIRouter()

    @router.api_route(
        "/probe", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
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


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_cookie_without_origin_is_403(method: str) -> None:
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


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_not_checked(method: str) -> None:
    response = _with_cookie().request(method, PROBE, headers={"Origin": "http://evil.example"})
    assert response.status_code == 200


def test_bearer_only_requests_are_not_checked() -> None:
    """비브라우저(ai·Updater·스크립트)는 쿠키 없이 Bearer만 보낸다 — Origin이 없어도 통과."""
    client = TestClient(_app())
    assert client.post(PROBE, headers={"Authorization": "Bearer t"}).status_code == 200
    assert client.delete(PROBE).status_code == 200


def test_login_with_origin_is_checked_even_without_cookie() -> None:
    """login CSRF: 다른 사이트가 공격자 계정으로 로그인시키는 것을 막는다."""
    client = TestClient(_app())
    assert client.post(LOGIN).status_code == 200  # Origin 없음 = 비브라우저
    assert client.post(LOGIN, headers={"Origin": SAME}).status_code == 200
    cross = client.post(LOGIN, headers={"Origin": "http://testserver:3000"})
    assert (cross.status_code, cross.json()) == (403, REJECTED)
    site = client.post(LOGIN, headers={"Origin": SAME, "Sec-Fetch-Site": "same-site"})
    assert site.status_code == 403
    assert _with_cookie().post(LOGIN).status_code == 403  # 쿠키가 실렸으면 Origin 필수


def test_rejection_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pureiron_core.api.origin"):
        _with_cookie().post(PROBE, headers={"Origin": "http://evil.example"})
    assert any(
        "rejected POST /api/v1/probe" in r.getMessage() and "evil.example" in r.getMessage()
        for r in caplog.records
    )


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
    ],
)
def test_same_origin_compares_host_and_port(
    origin: str, host: str, secure: bool, same: bool
) -> None:
    assert same_origin(origin, host, secure=secure) is same
