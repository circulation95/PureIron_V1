"""VM 쪽 현장 운영 값 PATCH(M5 확인 ⓑ). 현장 호스트에서 실행하고 JSON 한 줄을 출력한다.

    python3 site_api.py patch --username e2e-admin --password-file PW \\
        --tag line1/temp --tag-body '{"record": {...}}' --alarm temp-high --alarm-body '{...}'
    python3 site_api.py live --username e2e-viewer --password-file PW

브라우저와 같은 경로·헤더로 부른다 — web(nginx, `http://127.0.0.1`) 경유, 비안전 메서드마다
`Origin: http://127.0.0.1`(요청 `Host`와 같은 host:port)·`Sec-Fetch-Site: same-origin`, 로그인
응답의 세션 쿠키(`pureiron_session`, SameSite=Strict)를 PATCH에 싣는다(M3c-0 `OriginGuard`).
로그인은 한 번만 한다 — 실패가 쌓이면 같은 username이 잠시 막힌다(throttle). 비밀번호는 파일 첫
줄(VM에서 만든 값, 호스트로 나가지 않는다).

출력: `project_before`·`project_after`(`GET /api/v1/project`), `login`·`tag`·`alarm`(HTTP 상태와
본문) — 판정은 하네스(`run.judge_patch`)가 한다. 표준 라이브러리만 쓴다.
`--alarm2`·`--alarm2-body`를 주면 알람 PATCH를 하나 더 하고 `alarm2`로 낸다(G2-8 PATCH 3회 — 주지
않으면 출력이 그대로다).

`live`(G2-1): viewer로 로그인해 `GET /api/v1/tags`(태그 현재값·품질)·`GET /api/v1/devices`(장비
통신 상태 — 태그가 아니다)를 읽고 로그아웃한다. 출력 `login`·`tags`·`devices`(HTTP 상태와 본문),
판정은 하네스(`judges.judge_tags_good`·`judge_device_connected`)가 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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


Call = Callable[..., tuple[int, Any, list[str]]]


def login(args: argparse.Namespace, call: Call) -> tuple[dict[str, Any], str | None]:
    """한 번 로그인한다(실패가 쌓이면 throttle). (출력용 dict, 세션 쿠키 또는 None)."""
    password = Path(args.password_file).read_text(encoding="utf-8").splitlines()[0]
    status, body, set_cookies = call(
        "POST", "/api/v1/auth/login", {"username": args.username, "password": password}
    )
    token = session_of(set_cookies)
    # 토큰·비밀번호는 출력하지 않는다(로그에 남는 결과 파일)
    user: Any = cast(dict[str, Any], body).get("user") if isinstance(body, dict) else body
    out: dict[str, Any] = {"login": [status, {"user": user}], "session_cookie": token is not None}
    return out, token if status == 200 else None


def patch(args: argparse.Namespace, call: Call = call) -> dict[str, Any]:
    out: dict[str, Any] = {"project_before": call("GET", "/api/v1/project")[:2]}
    logged_in, token = login(args, call)
    out |= logged_in
    if token is not None:
        tag = urllib.parse.quote(args.tag, safe="/")
        tag_body = json.loads(args.tag_body)
        out["tag"] = call("PATCH", f"/api/v1/project/tags/{tag}", tag_body, token)[:2]
        out["alarm"] = call(
            "PATCH", f"/api/v1/project/alarms/{args.alarm}", json.loads(args.alarm_body), token
        )[:2]
        if args.alarm2 is not None:  # G2-8: 세 번째 PATCH(site_rev +3)
            out["alarm2"] = call(
                "PATCH",
                f"/api/v1/project/alarms/{args.alarm2}",
                json.loads(args.alarm2_body),
                token,
            )[:2]
        out["logout"] = call("POST", "/api/v1/auth/logout", None, token)[0]
    out["project_after"] = call("GET", "/api/v1/project")[:2]
    return out


def live(args: argparse.Namespace, call: Call = call) -> dict[str, Any]:
    out, token = login(args, call)
    if token is not None:
        out["tags"] = call("GET", "/api/v1/tags", None, token)[:2]
        out["devices"] = call("GET", "/api/v1/devices", None, token)[:2]
        out["logout"] = call("POST", "/api/v1/auth/logout", None, token)[0]
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
    cmd.add_argument("--alarm2", default=None)
    cmd.add_argument("--alarm2-body", default="{}")
    live_cmd = sub.add_parser("live")
    live_cmd.add_argument("--username", required=True)
    live_cmd.add_argument("--password-file", required=True)
    args = parser.parse_args()
    out = patch(args) if args.command == "patch" else live(args)
    print(json.dumps(out, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
