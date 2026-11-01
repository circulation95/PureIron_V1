"""VM 쪽 현장 운영 값 PATCH(M5 확인 ⓑ). 현장 호스트에서 실행하고 JSON 한 줄을 출력한다.

    python3 site_api.py patch --username e2e-admin --password-file PW \\
        --tag line1/temp --tag-body '{"record": {...}}' --alarm temp-high --alarm-body '{...}'

브라우저와 같은 경로·헤더로 부른다 — web(nginx, `http://127.0.0.1`) 경유, 비안전 메서드마다
`Origin: http://127.0.0.1`(요청 `Host`와 같은 host:port)·`Sec-Fetch-Site: same-origin`, 로그인
응답의 세션 쿠키(`pureiron_session`, SameSite=Strict)를 PATCH에 싣는다(M3c-0 `OriginGuard`).
로그인은 한 번만 한다 — 실패가 쌓이면 같은 username이 잠시 막힌다(throttle). 비밀번호는 파일 첫
줄(VM에서 만든 값, 호스트로 나가지 않는다).

출력: `project_before`·`project_after`(`GET /api/v1/project`), `login`·`tag`·`alarm`(HTTP 상태와
본문) — 판정은 하네스(`run.judge_patch`)가 한다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, cast

WEB = os.environ.get("PUREIRON_E2E_WEB", "http://127.0.0.1")
"""VM은 기본값(nginx :80). 로컬 사전 검증은 compose web 포트(`http://127.0.0.1:<port>`)로 바꾼다."""
SESSION_COOKIE = "pureiron_session"
BROWSER_HEADERS = {
    "Origin": WEB,
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
}


def call(
    method: str, path: str, body: object | None = None, cookie: str | None = None
) -> tuple[int, Any, list[str]]:
    """(상태, JSON 본문 또는 텍스트, 응답 `Set-Cookie` 헤더 전부). 비안전 메서드에는 브라우저
    헤더를 싣는다."""
    headers: dict[str, str] = {} if method == "GET" else dict(BROWSER_HEADERS)
    if cookie is not None:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{WEB}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw, status, got = response.read(), response.status, response.headers
    except urllib.error.HTTPError as exc:
        raw, status, got = exc.read(), exc.code, exc.headers
    try:
        parsed: Any = json.loads(raw or b"null")
    except ValueError:
        parsed = raw.decode(errors="replace")[:500]
    return status, parsed, [str(v) for v in got.get_all("Set-Cookie") or []]


def session_of(set_cookies: list[str]) -> str | None:
    """`Set-Cookie` 헤더들(쿠키가 여럿이면 헤더도 여럿)에서 세션 쿠키 값."""
    cookie = SimpleCookie()
    for header in set_cookies:
        cookie.load(header)
    morsel = cookie.get(SESSION_COOKIE)
    return None if morsel is None else morsel.value


def patch(args: argparse.Namespace) -> dict[str, Any]:
    password = Path(args.password_file).read_text(encoding="utf-8").splitlines()[0]
    out: dict[str, Any] = {"project_before": call("GET", "/api/v1/project")[:2]}
    status, body, set_cookies = call(
        "POST", "/api/v1/auth/login", {"username": args.username, "password": password}
    )
    token = session_of(set_cookies)
    # 토큰은 출력하지 않는다(로그에 남는 결과 파일)
    user: Any = cast(dict[str, Any], body).get("user") if isinstance(body, dict) else body
    out["login"] = [status, {"user": user}]
    out["session_cookie"] = token is not None
    if status == 200 and token is not None:
        tag = urllib.parse.quote(args.tag, safe="/")
        tag_body = json.loads(args.tag_body)
        out["tag"] = call("PATCH", f"/api/v1/project/tags/{tag}", tag_body, token)[:2]
        out["alarm"] = call(
            "PATCH", f"/api/v1/project/alarms/{args.alarm}", json.loads(args.alarm_body), token
        )[:2]
        out["logout"] = call("POST", "/api/v1/auth/logout", None, token)[0]
    out["project_after"] = call("GET", "/api/v1/project")[:2]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="site_api")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("patch")
    cmd.add_argument("--username", required=True)
    cmd.add_argument("--password-file", required=True)
    cmd.add_argument("--tag", required=True)
    cmd.add_argument("--tag-body", required=True)
    cmd.add_argument("--alarm", required=True)
    cmd.add_argument("--alarm-body", required=True)
    args = parser.parse_args()
    print(json.dumps(patch(args), sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
