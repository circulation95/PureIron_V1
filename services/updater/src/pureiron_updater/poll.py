"""control-api polling(현장발, plan §6.4) → 번들 다운로드(`fetched`, 창 밖 허용).

- `GET /sites/{site}/desired` → `{generation, bundle_url, sha256, size}`(ETag = gen, 304).
- 새 generation이면 `bundles/g<gen>.pib.part`에 **Range로 이어 받고**, 크기·sha256이 맞으면
  `g<gen>.pib`로 이름을 바꿔 시도로 등록한다. 틀리면 `.part`를 지우고 다음 poll에서 처음부터.
- 받기 전 여유 공간 ≥ 2배 번들 + 직전 백업 크기(PM-2 ⑥). 부족하면 받지 않고 기다린다.
- 커밋된 시도가 진행 중이면 polling은 아무것도 하지 않는다.

control-api는 M4에서 생긴다. 토큰은 `state/secrets/control_api_token`(PD-3, enroll 때 공급).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pureiron_bundle.verify import AttemptResult
from pureiron_updater.phases import Updater
from pureiron_updater.state import PRE_COMMIT

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
PART_SUFFIX = ".part"
TOKEN_FILE = "control_api_token"
_TIMEOUT_S = 30.0


class PollError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Desired:
    generation: int
    bundle_url: str
    sha256: str
    """`sha256:<hex>`."""
    size: int


def normalize_sha256(value: str) -> str:
    hexpart = value.removeprefix("sha256:").lower()
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        raise PollError(f"invalid sha256 {value!r}")
    return "sha256:" + hexpart


class ControlApi:
    def __init__(self, base_url: str, site_id: str, token_file: Path) -> None:
        self._base = base_url.rstrip("/")
        self._site = urllib.parse.quote(site_id, safe="")
        self._token_file = token_file
        self._etag: str | None = None
        self._last: Desired | None = None

    def _request(self, url: str, headers: dict[str, str]) -> urllib.request.Request:
        request = urllib.request.Request(urllib.parse.urljoin(self._base + "/", url))
        token = self._token_file.read_text(encoding="utf-8").strip()
        request.add_header("Authorization", f"Bearer {token}")
        for key, value in headers.items():
            request.add_header(key, value)
        return request

    def desired(self) -> Desired | None:
        headers = {"If-None-Match": self._etag} if self._etag else {}
        request = self._request(f"sites/{self._site}/desired", headers)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
                body = json.loads(response.read())
                self._etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return self._last
            if exc.code == 404:
                return None
            raise PollError(f"desired: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise PollError(f"desired: {exc}") from exc
        if not isinstance(body, dict):
            raise PollError("desired: not an object")
        data = cast(dict[str, object], body)
        gen, url, sha, size = (data.get(k) for k in ("generation", "bundle_url", "sha256", "size"))
        if not (isinstance(gen, int) and isinstance(url, str) and isinstance(sha, str)):
            raise PollError(f"desired: unexpected body {data}")
        if not isinstance(size, int) or size <= 0:
            raise PollError(f"desired: bad size {size!r}")
        self._last = Desired(gen, url, normalize_sha256(sha), size)
        return self._last

    def download(self, url: str, part: Path, size: int) -> None:
        """`part`에 이어 받는다(Range). 서버가 Range를 무시(200)하면 처음부터 다시 쓴다."""
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            part.unlink()
            offset = 0
        if offset == size:
            return
        request = self._request(url, {"Range": f"bytes={offset}-"} if offset else {})
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
                mode = "ab" if offset and response.status == 206 else "wb"
                with part.open(mode) as sink:
                    while chunk := response.read(CHUNK):
                        sink.write(chunk)
        except urllib.error.HTTPError as exc:
            raise PollError(f"download: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PollError(f"download interrupted (resumes next poll): {exc}") from exc


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _wanted(updater: Updater, desired: Desired) -> bool:
    state = updater.state
    gen = desired.generation
    known = [g for g in (state.applied_gen, state.last_attempted_gen) if g is not None]
    retried = gen == state.last_attempted_gen and state.last_result is not AttemptResult.PENDING
    if (known and gen < max(known)) or gen == state.applied_gen or retried:
        return False
    if desired.sha256 in state.skipped_bundles:
        return False
    attempt = state.attempt
    if attempt is None:
        return True
    if attempt.bundle_sha256 == desired.sha256:
        return False
    return attempt.generation is None or attempt.generation < gen


def _space_needed(updater: Updater, size: int) -> int:
    backups = updater.settings.backups_dir
    dumps = [p.stat().st_size for p in backups.glob("g*/db.dump")] if backups.is_dir() else []
    return 2 * size + max(dumps, default=0)


def poll_once(updater: Updater, api: ControlApi) -> bool:
    """새 번들을 받아 시도로 등록했으면 True."""
    attempt = updater.state.attempt
    if attempt is not None and attempt.phase not in PRE_COMMIT:
        return False
    desired = api.desired()
    if desired is None or not _wanted(updater, desired):
        return False
    bundles = updater.settings.bundles_dir
    bundles.mkdir(parents=True, exist_ok=True)
    final = bundles / f"g{desired.generation}.pib"
    part = final.with_name(final.name + PART_SUFFIX)
    have = part.stat().st_size if part.exists() else 0
    free = shutil.disk_usage(bundles).free
    needed = _space_needed(updater, desired.size) - have
    if free < needed:
        log.error(
            "not enough space for generation %s: free %d < %d", desired.generation, free, needed
        )
        return False
    api.download(desired.bundle_url, part, desired.size)
    got = part.stat().st_size
    if got < desired.size:
        # 연결이 끊겨 짧게 끝났다 — `.part`를 두고 다음 poll에서 Range로 잇는다.
        raise PollError(f"generation {desired.generation}: {got}/{desired.size} bytes, resuming")
    actual = file_sha256(part)
    if got != desired.size or actual != desired.sha256:
        part.unlink()
        raise PollError(f"generation {desired.generation}: downloaded {actual} != {desired.sha256}")
    part.replace(final)
    updater.submit(final, desired.sha256)
    return True
