"""현장 등록(PD-3)과 폐쇄망 보고 묶음(`report export`).

`state/` 아래 파일:

- `site.key` — site Ed25519 개인키(raw 32B seed, 0600). 새 파일로만 만들고 재등록은 tmp → rename.
- `site.json` — `{site_id, fingerprint, next_report_seq, seq_pending, enrolled_at}`, `report_seq`
  카운터의 정본. `seq_pending`은 상태를 잃고 재등록해 다음 seq를 아직 모르는 상태다.
- `enrollment.json` — `site-enrollment` 서명 봉투(자기 키 서명 = 개인키 보유 증명). 벤더가
  control-api `POST /sites`(신규)·`POST /sites/{site}/rekey`(재등록)로 등록한다(M4).
- `reports/*.json` — Updater 보고 대기열. `report export`가 `report_seq`를 매기고 site key로
  서명한 묶음을 쓴 뒤 원본을 `reports/exported/`로 옮긴다.

재등록 흐름: `enroll --re-enroll`(새 키) → 벤더가 rekey 등록 → 응답 `next_seq`를
`enroll --site <id> --next-seq N`으로 반영(키는 그대로, 카운터만 올림 — 단조 증가만 허용).
`site.json`이 남아 있으면 카운터를 이어 쓰므로 이 단계가 없어도 된다.

`report export`는 저널(`reports/.export.json`: 첫 seq + 묶을 파일 이름)을 먼저 쓰고 카운터를
예약한 뒤 묶음을 쓰고 원본을 옮긴다. 어디서 죽어도 다음 실행이 저널대로 같은 seq·같은 보고를
다시 내므로 seq가 재사용되거나 같은 보고가 다른 seq로 나가지 않는다.
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
from pureiron_updater.state import StateError, StateStore, atomic_write, fsync_dir

IDENTITY_FILE = "site.json"
ENROLLMENT_FILE = "enrollment.json"
EXPORTED_DIR = "exported"
JOURNAL_FILE = ".export.json"


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
    seq_pending: bool = False
    """상태 분실 재등록 뒤 벤더 rekey 응답의 `next_seq`를 아직 반영하지 않음(export 거부)."""


def load_identity(state_dir: Path) -> SiteIdentity | None:
    path = state_dir / IDENTITY_FILE
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EnrollError(f"{path}: unreadable ({exc})") from exc
    if not isinstance(data, dict):
        raise EnrollError(f"{path}: not a JSON object")
    doc = cast(dict[str, object], data)
    try:
        identity = SiteIdentity(
            site_id=str(doc["site_id"]),
            fingerprint=str(doc["fingerprint"]),
            next_report_seq=cast(int, doc["next_report_seq"]),
            enrolled_at=str(doc["enrolled_at"]),
            seq_pending=doc.get("seq_pending", False) is True,
        )
    except KeyError as exc:
        raise EnrollError(f"{path}: malformed (missing {exc})") from exc
    if type(identity.next_report_seq) is not int or identity.next_report_seq < 1:
        raise EnrollError(f"{path}: next_report_seq must be >= 1")
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


def _env_file_site(settings: Settings) -> str:
    """env 파일에 기록된 현장 id(서비스가 실제로 읽는 값). 없으면 빈 문자열."""
    try:
        text = settings.env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return parse_env_file(text).get(SITE_ID_VAR, "").strip()


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
        "seq_pending": identity.seq_pending,
        "created_at": format_timestamp(now),
        "updater_version": settings.updater_version,
    }
    path = settings.state_dir / ENROLLMENT_FILE
    atomic_write(path, dumps(sign_document(seed, SitePurpose.ENROLLMENT, payload)))
    return path


def _has_history(settings: Settings) -> bool:
    """이 호스트가 이미 번들을 적용·보고한 적이 있는지(키·`site.json`만 잃은 경우 감지)."""
    exported = settings.state_dir / REPORTS_DIR / EXPORTED_DIR
    if exported.is_dir() and any(exported.iterdir()):
        return True
    try:
        state = StateStore(settings.state_dir).load()
    except StateError:
        return True
    return state.applied_gen is not None or state.last_attempted_gen is not None


def _check_site(settings: Settings, site_id: str, identity: SiteIdentity | None) -> None:
    if not _site_id_pattern().fullmatch(site_id):
        raise EnrollError(f"invalid site id {site_id!r}")
    for where, configured in (
        (str(settings.env_file), _env_file_site(settings)),
        ("PUREIRON_SITE_ID", settings.site_id),
        (IDENTITY_FILE, identity.site_id if identity else ""),
    ):
        if configured and configured != site_id:
            raise EnrollError(
                f"this host is site {configured!r} ({where}); "
                "changing the site id needs a reinstall"
            )


def enroll(
    settings: Settings,
    site_id: str,
    *,
    now: datetime,
    re_enroll: bool = False,
    next_seq: int | None = None,
) -> Path:
    """site 키를 만들거나(신규·`re_enroll`) 그대로 두고 enrollment 파일 경로를 돌려준다.

    - 신규: 키가 없고 `site.json`도 없고 적용 이력도 없을 때만. seq는 1부터.
    - 같은 현장에서 플래그 없이: 키는 그대로 두고 enrollment 파일만 다시 쓴다(파일 분실 대비).
      `next_seq`를 주면 카운터만 그 값으로 올린다(벤더 rekey 응답 반영, 단조 증가).
    - `re_enroll`: 새 키쌍. seq는 `next_seq` > 남은 `site.json` 값 순으로 정하고, 둘 다 없으면
      `seq_pending`(벤더 응답을 `--next-seq`로 반영할 때까지 report export 거부).
    """
    state_dir, key_file = settings.state_dir, settings.site_key_file
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = load_identity(state_dir)
    _check_site(settings, site_id, identity)
    if next_seq is not None and next_seq < 1:
        raise EnrollError("--next-seq must be >= 1")
    if re_enroll:
        seed = _write_new_key(key_file)
        pending = next_seq is None and (identity is None or identity.seq_pending)
        seq = next_seq or (identity.next_report_seq if identity else 1)
        identity = SiteIdentity(
            site_id=site_id,
            fingerprint=keys.fingerprint(keys.public_key(seed)),
            next_report_seq=seq,
            enrolled_at=format_timestamp(now),
            seq_pending=pending,
        )
        _save_identity(state_dir, identity)
    elif identity is not None and key_file.exists():
        seed = keys.read_private_key(key_file)
        if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
            raise EnrollError(f"{key_file} does not match {IDENTITY_FILE} — use --re-enroll")
        if next_seq is not None:
            if not identity.seq_pending and next_seq < identity.next_report_seq:
                raise EnrollError(
                    f"--next-seq {next_seq} < current next_report_seq "
                    f"{identity.next_report_seq} (report_seq only moves forward)"
                )
            identity = replace(identity, next_report_seq=next_seq, seq_pending=False)
            _save_identity(state_dir, identity)
    elif identity is None and not key_file.exists():
        if next_seq is not None:
            raise EnrollError("--next-seq needs an enrolled site (or --re-enroll)")
        if _has_history(settings):
            raise EnrollError(
                "this host has applied bundles before but its site key is gone — "
                "use --re-enroll (the vendor re-registers the key and returns next_seq)"
            )
        seed = _write_new_key(key_file)
        identity = SiteIdentity(
            site_id=site_id,
            fingerprint=keys.fingerprint(keys.public_key(seed)),
            next_report_seq=1,
            enrolled_at=format_timestamp(now),
        )
        _save_identity(state_dir, identity)
    else:
        raise EnrollError(
            f"{key_file} and {IDENTITY_FILE} disagree (one is missing) — use --re-enroll"
        )
    if _env_file_site(settings) != site_id:
        set_env_value(settings.env_file, SITE_ID_VAR, site_id)
    return _enrollment(settings, seed, identity, now)


# ── report export ─────────────────────────────────────────────────────────────


def _load_journal(path: Path) -> tuple[int, list[str]] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EnrollError(f"{path}: unreadable export journal ({exc})") from exc
    doc = cast(dict[str, object], data) if isinstance(data, dict) else {}
    first, names = doc.get("first_seq"), doc.get("names")
    if type(first) is not int or not isinstance(names, list):
        raise EnrollError(f"{path}: malformed export journal")
    return first, [str(n) for n in cast(list[object], names)]


def _read_report(queue: Path, name: str) -> dict[str, Json]:
    for path in (queue / name, queue / EXPORTED_DIR / name):
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise EnrollError(f"{path}: unreadable report ({exc})") from exc
        if not isinstance(data, dict):
            raise EnrollError(f"{path}: report is not a JSON object")
        return cast(dict[str, Json], data)
    raise EnrollError(f"report {name} listed in the export journal is missing")


def export_reports(settings: Settings, out_dir: Path, *, now: datetime) -> tuple[Path, int] | None:
    """대기 중 보고에 `report_seq`를 매겨 서명 묶음을 `out_dir`에 쓴다. 보고가 없으면 None.

    끊긴 export(저널 있음)가 있으면 새 보고보다 먼저 그것을 같은 seq로 끝낸다.
    """
    state_dir = settings.state_dir
    identity = load_identity(state_dir)
    if identity is None:
        raise EnrollError("not enrolled (run: pureiron-site enroll --site <id>)")
    if identity.seq_pending:
        raise EnrollError(
            "report_seq is unknown after re-enrollment — apply the vendor's rekey answer first: "
            f"pureiron-site enroll --site {identity.site_id} --next-seq <next_seq>"
        )
    seed = keys.read_private_key(settings.site_key_file)
    if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
        raise EnrollError(f"{settings.site_key_file} does not match {IDENTITY_FILE}")
    queue = state_dir / REPORTS_DIR
    journal_file = queue / JOURNAL_FILE
    journal = _load_journal(journal_file)
    if journal is None:
        names = (
            sorted(p.name for p in queue.glob("*.json") if p.name != JOURNAL_FILE)
            if queue.is_dir()
            else []
        )
        if not names:
            return None
        journal = (identity.next_report_seq, names)
        body = {"first_seq": journal[0], "names": names}
        atomic_write(journal_file, (json.dumps(body, indent=2) + "\n").encode())
    first, names = journal
    last = first + len(names) - 1
    if identity.next_report_seq <= last:  # seq 예약(저널 기록 뒤 여기서 죽어도 재실행이 다시 한다)
        identity = replace(identity, next_report_seq=last + 1)
        _save_identity(state_dir, identity)
    reports: list[Json] = [
        {**_read_report(queue, name), "report_seq": first + i} for i, name in enumerate(names)
    ]
    payload: dict[str, Json] = {
        "site_id": identity.site_id,
        "key_fingerprint": identity.fingerprint,
        "exported_at": format_timestamp(now),
        "first_seq": first,
        "last_seq": last,
        "reports": reports,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"pureiron-reports-{identity.site_id}-{first:06d}-{last:06d}.json"
    atomic_write(target, dumps(sign_document(seed, SitePurpose.REPORTS, payload)))
    exported = queue / EXPORTED_DIR
    exported.mkdir(exist_ok=True)
    for name in names:
        source = queue / name
        if source.exists():
            source.replace(exported / name)
    journal_file.unlink()
    fsync_dir(queue)
    return target, len(names)
