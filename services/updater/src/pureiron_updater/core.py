"""core HTTP 클라이언트(`/api/v1`, 호스트에서는 web nginx 프록시 경유) — 표준 라이브러리만.

편집 잠금은 Updater 토큰(`state/secrets/updater_token`, core 컨테이너와 같은 파일)으로 호출한다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import cast

from pureiron_updater.ports import LOCK_OWNER, CoreUnavailableError, LockConflictError, Readiness

_TIMEOUT_S = 10.0


def _read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CoreUnavailableError(f"updater token unreadable: {exc}") from exc


class CoreHttp:
    def __init__(self, base_url: str, token_file: Path) -> None:
        self._base = base_url.rstrip("/")
        self._token_file = token_file

    def _request(
        self, method: str, path: str, body: object | None = None, *, auth: bool = False
    ) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self._base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth:
            request.add_header("Authorization", f"Bearer {_read_token(self._token_file)}")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
                status, raw = int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise CoreUnavailableError(f"{method} {path}: {exc}") from exc
        try:
            payload: object = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return status, payload

    def _ok(self, method: str, path: str, status: int, payload: object) -> dict[str, object]:
        if status == 409:
            raise LockConflictError(f"{method} {path}: {payload}")
        if not 200 <= status < 300:
            raise CoreUnavailableError(f"{method} {path}: HTTP {status} {payload}")
        return cast(dict[str, object], payload) if isinstance(payload, dict) else {}

    def acquire_lock(self, generation: int) -> int:
        body = {"owner": LOCK_OWNER, "generation": generation}
        status, payload = self._request("POST", "/project/edit-lock", body, auth=True)
        site_rev = self._ok("POST", "/project/edit-lock", status, payload).get("site_rev")
        if not isinstance(site_rev, int):
            raise CoreUnavailableError(f"edit-lock response without site_rev: {payload}")
        return site_rev

    def release_lock(self) -> None:
        path = "/project/edit-lock?" + urllib.parse.urlencode({"owner": LOCK_OWNER})
        status, payload = self._request("DELETE", path, auth=True)
        self._ok("DELETE", path, status, payload)

    def project(self) -> tuple[int, int]:
        status, payload = self._request("GET", "/project")
        data = self._ok("GET", "/project", status, payload)
        site_rev, base = data.get("site_rev"), data.get("base_site_rev")
        if not isinstance(site_rev, int) or not isinstance(base, int):
            raise CoreUnavailableError(f"/project without site_rev/base_site_rev: {payload}")
        return site_rev, base

    def readiness(self) -> Readiness:
        status, payload = self._request("GET", "/readyz")
        if status >= 500 and not isinstance(payload, dict):
            raise CoreUnavailableError(f"GET /readyz: HTTP {status}")
        data = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
        checks = data.get("checks")
        collector = False
        if isinstance(checks, dict):
            entry = cast(dict[str, object], checks).get("collector")
            collector = isinstance(entry, dict) and cast(dict[str, object], entry).get("ok") is True
        # GOOD 비율은 core가 태그 품질 지표를 내보내는 M3a부터 채운다(그 전엔 baseline도 None).
        return Readiness(ready=status == 200 and data.get("status") == "ready", collector=collector)
