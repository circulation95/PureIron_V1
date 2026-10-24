"""compose 인증 흐름(M3b-1): CLI로 만든 계정 → web 프록시로 로그인(쿠키) → me(쿠키·Bearer) →
감사 조회(engineer+, viewer는 403) → 로그아웃 후 401.
M3c-0: web 프록시를 거친 쿠키 POST의 Origin 검사, nginx 보안 헤더, 로그인 실패 제한 429 + 감사.

기본 계정은 없다 — 테스트가 CLI(`docker compose exec core pureiron-core users add`)로 만든다(D3).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import cast

import pytest

from .conftest import CORE_PORT, WEB_PORT, Stack, compose, wait_http

pytestmark = pytest.mark.integration

CORE = f"http://127.0.0.1:{CORE_PORT}/api/v1"
WEB = f"http://127.0.0.1:{WEB_PORT}/api/v1"
ADMIN, VIEWER = "it-admin", "it-viewer"
PASSWORD = "it-pass-1234"

type Json = dict[str, object]


def _call(
    method: str,
    url: str,
    *,
    body: Json | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Mapping[str, str], Json]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw: bytes = response.read()
            status: int = response.status
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raw, status, response_headers = (
            exc.read(),
            exc.code,
            {k.lower(): v for k, v in exc.headers.items()},
        )
    payload = cast("Json", json.loads(raw)) if raw else {}
    return status, response_headers, payload


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(
    base: str, username: str, password: str = PASSWORD
) -> tuple[int, Mapping[str, str], Json]:
    return _call("POST", f"{base}/auth/login", body={"username": username, "password": password})


def _entries(payload: Json) -> list[Json]:
    return cast("list[Json]", payload["entries"])


@pytest.fixture(scope="module")
def accounts(stack: Stack) -> None:
    wait_http(f"{CORE}/readyz", timeout_s=120)
    for username, role in ((ADMIN, "admin"), (VIEWER, "viewer")):
        added = compose(
            stack.compose_env,
            "exec",
            "-T",
            "core",
            "pureiron-core",
            "users",
            "add",
            username,
            "--role",
            role,
            check=False,
            stdin=f"{PASSWORD}\n",
        )
        assert added.returncode == 0, added.stdout + added.stderr


@pytest.mark.compose
def test_login_me_audit_logout(accounts: None) -> None:
    status, headers, body = _login(WEB, ADMIN)  # web(nginx) 같은 origin 프록시 경유
    assert status == 200, body
    token = str(body["token"])
    cookie = headers["set-cookie"].lower()
    assert cookie.startswith(f"pureiron_session={token.lower()};")
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api" in cookie
    assert "secure" not in cookie  # 현장 기본값(http)

    status, _, me = _call("GET", f"{WEB}/auth/me", headers={"Cookie": f"pureiron_session={token}"})
    assert (status, me["username"], me["role"]) == (200, ADMIN, "admin")
    status, _, me = _call("GET", f"{CORE}/auth/me", headers=_bearer(token))
    assert (status, me["username"]) == (200, ADMIN)

    status, _, _ = _login(CORE, ADMIN, "wrong-pass-9")
    assert status == 401

    status, _, audit = _call("GET", f"{CORE}/audit?username={ADMIN}", headers=_bearer(token))
    assert status == 200, audit
    actions = [(e["action"], e["source"]) for e in _entries(audit)]
    assert actions[:2] == [("auth.login_failed", "api"), ("auth.login", "api")]
    failed = _entries(audit)[0]
    assert failed["detail"] == {"reason": "bad_password"}
    assert "wrong-pass-9" not in json.dumps(audit)

    status, _, created = _call("GET", f"{CORE}/audit?action=users.add", headers=_bearer(token))
    assert status == 200
    assert {(e["target"], e["username"], e["source"]) for e in _entries(created)} == {
        (ADMIN, None, "cli"),
        (VIEWER, None, "cli"),
    }

    status, _, viewer = _login(CORE, VIEWER)
    assert status == 200
    status, _, _ = _call("GET", f"{CORE}/audit", headers=_bearer(str(viewer["token"])))
    assert status == 403

    status, _, _ = _call("POST", f"{CORE}/auth/logout", headers=_bearer(token))
    assert status == 204
    status, _, _ = _call("GET", f"{CORE}/auth/me", headers=_bearer(token))
    assert status == 401
    status, _, _ = _call("GET", f"{CORE}/audit", headers=_bearer(token))
    assert status == 401


@pytest.mark.compose
def test_cookie_post_through_web_needs_same_origin(accounts: None) -> None:
    """nginx가 브라우저 Host를 그대로 넘기므로 web origin과 Host가 맞는다(plan M3c D14)."""
    status, _, body = _login(WEB, VIEWER)
    assert status == 200, body
    token = str(body["token"])
    cookie = {"Cookie": f"pureiron_session={token}"}
    status, _, rejected = _call("POST", f"{WEB}/auth/logout", headers=cookie)
    assert (status, rejected) == (403, {"detail": "E_ORIGIN"})
    other_port = {**cookie, "Origin": "http://127.0.0.1:3000"}
    assert _call("POST", f"{WEB}/auth/logout", headers=other_port)[0] == 403
    assert _call("GET", f"{CORE}/auth/me", headers=_bearer(token))[0] == 200  # 아직 살아 있다
    same = {**cookie, "Origin": f"http://127.0.0.1:{WEB_PORT}", "Sec-Fetch-Site": "same-origin"}
    assert _call("POST", f"{WEB}/auth/logout", headers=same)[0] == 204
    assert _call("GET", f"{CORE}/auth/me", headers=_bearer(token))[0] == 401


def _response_headers(url: str) -> tuple[int, Mapping[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


@pytest.mark.compose
def test_web_sends_security_headers(accounts: None) -> None:
    """정적 화면·API 프록시·오류 응답 모두 보안 헤더가 붙는다(plan M3c D15)."""
    expected = {
        "content-security-policy": "frame-ancestors 'none'",
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "same-origin",
    }
    root = f"http://127.0.0.1:{WEB_PORT}"
    for url, want in ((f"{root}/", 200), (f"{root}/alarms", 200), (f"{WEB}/auth/me", 401)):
        status, headers = _response_headers(url)
        assert status == want, url
        assert {k: headers.get(k) for k in expected} == expected, url


@pytest.mark.compose
def test_repeated_failed_logins_are_throttled_and_audited(accounts: None) -> None:
    """같은 username 5회 실패 → 429 + Retry-After, 감사 `auth.login_throttled` 한 건(M3c D16)."""
    name = "it-throttled"  # 없는 계정도 같은 규칙으로 센다
    assert [_login(CORE, name, "wrong-pass-9")[0] for _ in range(5)] == [401] * 5
    status, headers, body = _login(CORE, name, "wrong-pass-9")
    assert status == 429, body
    assert 1 <= int(headers["retry-after"]) <= 600
    status, _, admin = _login(CORE, ADMIN)
    assert status == 200  # 다른 username은 영향 없다
    status, _, audit = _call(
        "GET",
        f"{CORE}/audit?action=auth.login_throttled&username={name}",
        headers=_bearer(str(admin["token"])),
    )
    assert status == 200, audit
    entries = _entries(audit)
    assert [(e["target"], e["source"]) for e in entries] == [(name, "api")]
    assert entries[0]["detail"] == {"failures": 5, "window_s": 600.0, "block_s": 600.0}
