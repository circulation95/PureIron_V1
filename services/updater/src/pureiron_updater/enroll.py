"""현장 등록(PD-3)과 폐쇄망 보고 묶음(`report export`).

`state/` 아래 파일:

- `site.key` — site Ed25519 개인키(raw 32B seed, 0600). 새 파일로만 만들고 재등록은 tmp → rename.
- `site.json` — `{site_id, fingerprint, next_report_seq, enrolled_at}`, `report_seq` 카운터의 정본.
- `enrollment.json` — `site-enrollment` 서명 봉투(자기 키 서명 = 개인키 보유 증명). 벤더가
  control-api `POST /sites`(신규)·`POST /sites/{site}/rekey`(재등록)로 등록한다(M4).
- `reports/*.json` — Updater 보고 대기열. `report export`가 `report_seq`를 매기고 site key로
  서명한 묶음을 쓴 뒤 원본을 `reports/exported/`로 옮긴다.

`report_seq`는 단조 증가다. 묶음 파일을 쓴 뒤 카운터를 저장하고 마지막에 원본을 옮긴다 —
그 사이에 죽으면 다음 묶음에 같은 보고가 새 seq로 다시 들어갈 수 있다(중복은 수신 측이
`bundle_sha256`·`started_at`으로 거른다). 반대 순서는 seq 재사용(이력으로만 저장됨)이 생겨
더 나쁘다.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json, SchemaName, contract_schema
from pureiron_bundle.sitesign import SitePurpose, dumps, sign_document
from pureiron_bundle.trust import format_timestamp
from pureiron_updater.metrics import REPORTS_DIR
from pureiron_updater.settings import SITE_ID_VAR, Settings, parse_env_file
from pureiron_updater.state import atomic_write, fsync_dir

IDENTITY_FILE = "site.json"
ENROLLMENT_FILE = "enrollment.json"
EXPORTED_DIR = "exported"


class EnrollError(Exception):
    pass


@cache
def _site_id_pattern() -> re.Pattern[str]:
    defs = cast(Mapping[str, Mapping[str, str]], contract_schema(SchemaName.FLEET_SITE)["$defs"])
    return re.compile(defs["id"]["pattern"])


@dataclass(frozen=True, slots=True)
class SiteIdentity:
    site_id: str
    fingerprint: str
    next_report_seq: int
    enrolled_at: str


def load_identity(state_dir: Path) -> SiteIdentity | None:
    try:
        data: object = json.loads((state_dir / IDENTITY_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EnrollError(f"{state_dir / IDENTITY_FILE}: unreadable ({exc})") from exc
    try:
        doc = cast(dict[str, object], data)
        identity = SiteIdentity(
            site_id=str(doc["site_id"]),
            fingerprint=str(doc["fingerprint"]),
            next_report_seq=cast(int, doc["next_report_seq"]),
            enrolled_at=str(doc["enrolled_at"]),
        )
    except (KeyError, TypeError) as exc:
        raise EnrollError(f"{state_dir / IDENTITY_FILE}: malformed ({exc})") from exc
    if type(identity.next_report_seq) is not int or identity.next_report_seq < 1:
        raise EnrollError(f"{state_dir / IDENTITY_FILE}: next_report_seq must be >= 1")
    return identity


def _save_identity(state_dir: Path, identity: SiteIdentity) -> None:
    text = json.dumps(asdict(identity), indent=2, sort_keys=True) + "\n"
    atomic_write(state_dir / IDENTITY_FILE, text.encode())


def set_env_value(path: Path, key: str, value: str) -> None:
    """env 파일의 `KEY=` 줄을 바꾸거나 덧붙인다(다른 줄·주석 보존, 원자적 교체)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    entry = f"{key}={value}"
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.strip().partition("=")[0].strip() == key and not line.lstrip().startswith("#"):
            if not replaced:
                out.append(entry)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, ("\n".join(out) + "\n").encode())


def _configured_site(settings: Settings) -> str:
    try:
        text = settings.env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return settings.site_id
    return settings.site_id or parse_env_file(text).get(SITE_ID_VAR, "").strip()


def _write_new_key(path: Path) -> bytes:
    seed = keys.generate_seed()
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    keys.write_private_key(tmp, seed)
    with tmp.open("r+b") as handle:  # Windows는 읽기 전용 fd의 fsync를 거부한다
        os.fsync(handle.fileno())
    tmp.replace(path)
    fsync_dir(path.parent)
    return seed


def _enrollment(settings: Settings, seed: bytes, identity: SiteIdentity, now: datetime) -> Path:
    public = keys.public_key(seed)
    payload: dict[str, Json] = {
        "site_id": identity.site_id,
        "public_key": public.hex(),
        "fingerprint": keys.fingerprint(public),
        "next_report_seq": identity.next_report_seq,
        "created_at": format_timestamp(now),
        "updater_version": settings.updater_version,
    }
    path = settings.state_dir / ENROLLMENT_FILE
    atomic_write(path, dumps(sign_document(seed, SitePurpose.ENROLLMENT, payload)))
    return path


def enroll(
    settings: Settings,
    site_id: str,
    *,
    now: datetime,
    re_enroll: bool = False,
    next_seq: int | None = None,
) -> Path:
    """site 키를 만들고(재등록이면 새로) enrollment 파일 경로를 돌려준다.

    이미 등록된 같은 현장에서 플래그 없이 부르면 키는 그대로 두고 enrollment 파일만 다시 쓴다
    (파일 분실 대비). `next_seq`는 재등록 때 벤더 rekey 응답의 `next_seq` — 상태(`site.json`)를
    잃은 재등록에는 필수다(1부터 다시 세면 control-api가 새 보고를 이력으로만 저장한다).
    """
    configured = _configured_site(settings)
    state_dir, key_file = settings.state_dir, settings.site_key_file
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = load_identity(state_dir)
    _check_request(site_id, configured, identity, re_enroll=re_enroll, next_seq=next_seq)
    if not re_enroll and key_file.exists() != (identity is not None):
        raise EnrollError(
            f"{key_file} and {IDENTITY_FILE} disagree (one is missing) — use --re-enroll"
        )
    if not re_enroll and identity is not None:
        seed = keys.read_private_key(key_file)
        if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
            raise EnrollError(f"{key_file} does not match {IDENTITY_FILE} — use --re-enroll")
    else:
        if next_seq is None:
            next_seq = 1 if identity is None else identity.next_report_seq
        seed = _write_new_key(key_file)
        identity = SiteIdentity(
            site_id=site_id,
            fingerprint=keys.fingerprint(keys.public_key(seed)),
            next_report_seq=next_seq,
            enrolled_at=format_timestamp(now),
        )
        _save_identity(state_dir, identity)
    if not configured:
        set_env_value(settings.env_file, SITE_ID_VAR, site_id)
    return _enrollment(settings, seed, identity, now)


def _check_request(
    site_id: str,
    configured: str,
    identity: SiteIdentity | None,
    *,
    re_enroll: bool,
    next_seq: int | None,
) -> None:
    if not _site_id_pattern().fullmatch(site_id):
        raise EnrollError(f"invalid site id {site_id!r}")
    if configured and configured != site_id:
        raise EnrollError(
            f"this host is configured as site {configured!r}; "
            "changing the site id needs a reinstall"
        )
    if identity is not None and identity.site_id != site_id:
        raise EnrollError(f"state belongs to site {identity.site_id!r}, not {site_id!r}")
    if next_seq is not None and (not re_enroll or next_seq < 1):
        raise EnrollError("--next-seq must be >= 1 and is only valid with --re-enroll")
    if re_enroll and identity is None and next_seq is None:
        raise EnrollError("site state is lost — pass --next-seq from the vendor's rekey answer")


def export_reports(settings: Settings, out_dir: Path, *, now: datetime) -> tuple[Path, int] | None:
    """대기 중 보고에 `report_seq`를 매겨 서명 묶음을 `out_dir`에 쓴다. 보고가 없으면 None."""
    state_dir = settings.state_dir
    identity = load_identity(state_dir)
    if identity is None:
        raise EnrollError("not enrolled (run: pureiron-site enroll --site <id>)")
    seed = keys.read_private_key(settings.site_key_file)
    if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
        raise EnrollError(f"{settings.site_key_file} does not match {IDENTITY_FILE}")
    queue = state_dir / REPORTS_DIR
    pending = sorted(queue.glob("*.json")) if queue.is_dir() else []
    if not pending:
        return None
    first = seq = identity.next_report_seq
    reports: list[Json] = []
    for path in pending:
        try:
            report = cast(dict[str, Json], json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise EnrollError(f"{path}: unreadable report ({exc})") from exc
        reports.append({**report, "report_seq": seq})
        seq += 1
    payload: dict[str, Json] = {
        "site_id": identity.site_id,
        "key_fingerprint": identity.fingerprint,
        "exported_at": format_timestamp(now),
        "first_seq": first,
        "last_seq": seq - 1,
        "reports": reports,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"pureiron-reports-{identity.site_id}-{first:06d}-{seq - 1:06d}.json"
    atomic_write(target, dumps(sign_document(seed, SitePurpose.REPORTS, payload)))
    _save_identity(state_dir, replace(identity, next_report_seq=seq))
    exported = queue / EXPORTED_DIR
    exported.mkdir(exist_ok=True)
    for path in pending:
        path.replace(exported / path.name)
    fsync_dir(queue)
    return target, len(pending)
