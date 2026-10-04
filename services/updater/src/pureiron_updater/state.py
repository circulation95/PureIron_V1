"""Updater 상태 파일 `state/updater.json`(plan M2 B4·B11).

기록은 항상 tmp write → `fsync(file)` → `rename` → `fsync(dir)`이라 crash 뒤에는 이전 상태나 새 상태
중 하나만 남는다. 저장된 `attempt.phase`는 **지금 실행 중인 단계(의도)**다 — `recover()`는 그 단계를
멱등하게 다시 실행하거나(`migrating` 이후), 서비스 영향 이전이면 대기로 되돌린다.

같은 상태 디렉터리를 두 프로세스가 동시에 다루지 않도록 `updater.lock`(flock)을 쓴다.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, cast

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from pureiron_bundle.trust import TrustState, format_timestamp, parse_timestamp
from pureiron_bundle.verify import AttemptResult

STATE_FORMAT = 1
STATE_FILE = "updater.json"
LOCK_FILE = "updater.lock"
SKIPPED_KEEP = 20


class Phase(StrEnum):
    FETCHED = "fetched"
    VERIFIED = "verified"
    COMMITTED = "committed"
    LOCKED = "locked"
    BACKED_UP = "backed_up"
    MIGRATING = "migrating"
    SWITCHING = "switching"
    CHECKING = "checking"
    SUCCEEDED = "succeeded"
    ROLLING_BACK = "rolling_back"
    FAILED = "failed"
    REJECTED = "rejected"
    FAILED_MANUAL = "failed_manual"


TERMINAL = frozenset({Phase.SUCCEEDED, Phase.FAILED, Phase.REJECTED, Phase.FAILED_MANUAL})
PRE_COMMIT = frozenset({Phase.FETCHED, Phase.VERIFIED})
"""창 밖에서도 머무를 수 있고, 더 높은 generation이 오면 버려도 되는 단계."""


class StateError(Exception):
    """상태 파일이 깨졌거나 형식이 맞지 않는다(자동 복구 대상 아님 — 운영자 확인)."""


@dataclass(frozen=True, slots=True)
class Attempt:
    """적용 시도 하나. generation은 검증 전에는 모른다(None)."""

    phase: Phase
    bundle: str
    """`.pib` 경로(`bundles/` 아래 사본)."""
    bundle_sha256: str
    extract_dir: str
    started_at: datetime
    generation: int | None = None
    from_generation: int | None = None
    first_install: bool = False
    migrate_to: int | None = None
    """`migrate up --to` 값(스키마 판정이 MIGRATE일 때만)."""
    trust_after: TrustState | None = None
    baseline_good_ratio: float | None = None
    lock_held: bool = False
    failed_phase: Phase | None = None
    code: str | None = None
    detail: str | None = None
    rollback_tries: int = 0
    verify_s: float | None = None
    load_s: float | None = None
    timeline: Mapping[str, datetime] = field(default_factory=dict[str, datetime])
    """phase → 마지막으로 그 phase에 들어간 시각(G2-2 `locked`→`succeeded` 측정)."""


@dataclass(frozen=True, slots=True)
class UpdaterState:
    applied_gen: int | None = None
    """마지막으로 성공한 generation(`compose/g<gen>/`이 그 정의)."""
    last_attempted_gen: int | None = None
    last_result: AttemptResult | None = None
    schema_epoch: int | None = None
    """Updater가 마지막으로 확인한 DB 스키마 epoch(마이그레이션은 Updater만 한다)."""
    trust: TrustState = field(default_factory=TrustState)
    attempt: Attempt | None = None
    skipped_bundles: tuple[str, ...] = ()
    """커밋 전에 끝난(거부·적재 실패) 번들 sha256 — 같은 파일을 다시 받지 않는다."""
    alarm: str | None = None
    """`failed_manual`로 끝난 시도의 요약. 다음 성공 적용에서 지운다."""


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _trust_json(trust: TrustState) -> dict[str, object]:
    seen = trust.max_seen_created_at
    return {
        "min_cert_serial": trust.min_cert_serial,
        "max_seen_created_at": None if seen is None else format_timestamp(seen),
    }


def _attempt_json(attempt: Attempt) -> dict[str, object]:
    return {
        "phase": attempt.phase.value,
        "bundle": attempt.bundle,
        "bundle_sha256": attempt.bundle_sha256,
        "extract_dir": attempt.extract_dir,
        "started_at": _ts(attempt.started_at),
        "generation": attempt.generation,
        "from_generation": attempt.from_generation,
        "first_install": attempt.first_install,
        "migrate_to": attempt.migrate_to,
        "trust_after": None if attempt.trust_after is None else _trust_json(attempt.trust_after),
        "baseline_good_ratio": attempt.baseline_good_ratio,
        "lock_held": attempt.lock_held,
        "failed_phase": None if attempt.failed_phase is None else attempt.failed_phase.value,
        "code": attempt.code,
        "detail": attempt.detail,
        "rollback_tries": attempt.rollback_tries,
        "verify_s": attempt.verify_s,
        "load_s": attempt.load_s,
        "timeline": {phase: ts.isoformat() for phase, ts in attempt.timeline.items()},
    }


def to_json(state: UpdaterState) -> dict[str, object]:
    return {
        "format": STATE_FORMAT,
        "applied_gen": state.applied_gen,
        "last_attempted_gen": state.last_attempted_gen,
        "last_result": None if state.last_result is None else state.last_result.value,
        "schema_epoch": state.schema_epoch,
        "trust": _trust_json(state.trust),
        "attempt": None if state.attempt is None else _attempt_json(state.attempt),
        "skipped_bundles": list(state.skipped_bundles),
        "alarm": state.alarm,
    }


class _Reader:
    """타입이 맞지 않으면 `StateError`. pyright strict에서 cast를 한곳에 모은다."""

    def __init__(self, data: object, where: str) -> None:
        if not isinstance(data, dict):
            raise StateError(f"{where}: not an object")
        self._data = cast(dict[str, object], data)
        self._where = where

    def raw(self, key: str) -> object:
        return self._data.get(key)

    def opt_int(self, key: str) -> int | None:
        value = self._data.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise StateError(f"{self._where}.{key}: not an integer")
        return value

    def integer(self, key: str, default: int = 0) -> int:
        value = self.opt_int(key)
        return default if value is None else value

    def opt_float(self, key: str) -> float | None:
        value = self._data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StateError(f"{self._where}.{key}: not a number")
        return float(value)

    def opt_str(self, key: str) -> str | None:
        value = self._data.get(key)
        if value is not None and not isinstance(value, str):
            raise StateError(f"{self._where}.{key}: not a string")
        return value

    def text(self, key: str) -> str:
        value = self.opt_str(key)
        if value is None:
            raise StateError(f"{self._where}.{key}: missing")
        return value

    def flag(self, key: str) -> bool:
        value = self._data.get(key, False)
        if not isinstance(value, bool):
            raise StateError(f"{self._where}.{key}: not a boolean")
        return value

    def opt_time(self, key: str) -> datetime | None:
        text = self.opt_str(key)
        if text is None:
            return None
        try:
            return parse_timestamp(text)
        except ValueError as exc:
            raise StateError(f"{self._where}.{key}: {exc}") from exc


def _trust_from(data: object, where: str) -> TrustState:
    reader = _Reader(data, where)
    return TrustState(reader.integer("min_cert_serial"), reader.opt_time("max_seen_created_at"))


def _phase(text: str | None, where: str) -> Phase | None:
    if text is None:
        return None
    try:
        return Phase(text)
    except ValueError as exc:
        raise StateError(f"{where}: unknown phase {text!r}") from exc


def _attempt_from(data: object) -> Attempt:
    r = _Reader(data, "attempt")
    phase = _phase(r.text("phase"), "attempt.phase")
    started = r.opt_time("started_at")
    if phase is None or started is None:
        raise StateError("attempt: phase/started_at missing")
    timeline_raw = _Reader(r.raw("timeline") or {}, "attempt.timeline")
    timeline: dict[str, datetime] = {}
    for key in Phase:
        stamp = timeline_raw.opt_time(key.value)
        if stamp is not None:
            timeline[key.value] = stamp
    trust_raw = r.raw("trust_after")
    return Attempt(
        phase=phase,
        bundle=r.text("bundle"),
        bundle_sha256=r.text("bundle_sha256"),
        extract_dir=r.text("extract_dir"),
        started_at=started,
        generation=r.opt_int("generation"),
        from_generation=r.opt_int("from_generation"),
        first_install=r.flag("first_install"),
        migrate_to=r.opt_int("migrate_to"),
        trust_after=None if trust_raw is None else _trust_from(trust_raw, "attempt.trust_after"),
        baseline_good_ratio=r.opt_float("baseline_good_ratio"),
        lock_held=r.flag("lock_held"),
        failed_phase=_phase(r.opt_str("failed_phase"), "attempt.failed_phase"),
        code=r.opt_str("code"),
        detail=r.opt_str("detail"),
        rollback_tries=r.integer("rollback_tries"),
        verify_s=r.opt_float("verify_s"),
        load_s=r.opt_float("load_s"),
        timeline=timeline,
    )


def from_json(data: object) -> UpdaterState:
    r = _Reader(data, "updater.json")
    if r.opt_int("format") != STATE_FORMAT:
        raise StateError(f"updater.json: unsupported format {r.raw('format')!r}")
    result_text = r.opt_str("last_result")
    try:
        last_result = None if result_text is None else AttemptResult(result_text)
    except ValueError as exc:
        raise StateError(f"updater.json.last_result: {result_text!r}") from exc
    skipped: object = r.raw("skipped_bundles") or []
    if not isinstance(skipped, list) or not all(
        isinstance(item, str) for item in cast(list[object], skipped)
    ):
        raise StateError("updater.json.skipped_bundles: not a list of strings")
    attempt_raw = r.raw("attempt")
    return UpdaterState(
        applied_gen=r.opt_int("applied_gen"),
        last_attempted_gen=r.opt_int("last_attempted_gen"),
        last_result=last_result,
        schema_epoch=r.opt_int("schema_epoch"),
        trust=_trust_from(r.raw("trust") or {}, "updater.json.trust"),
        attempt=None if attempt_raw is None else _attempt_from(attempt_raw),
        skipped_bundles=tuple(cast(list[str], skipped)),
        alarm=r.opt_str("alarm"),
    )


def fsync_dir(directory: Path) -> None:
    """rename을 디렉터리 엔트리까지 내구화한다(POSIX). Windows는 디렉터리 fsync가 없다."""
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """tmp write → fsync(file) → rename → fsync(dir)."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    fsync_dir(path.parent)


def skip_bundle(state: UpdaterState, sha256: str) -> UpdaterState:
    kept = tuple(s for s in state.skipped_bundles if s != sha256)
    return replace(state, skipped_bundles=(*kept, sha256)[-SKIPPED_KEEP:])


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STATE_FILE

    def load(self) -> UpdaterState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return UpdaterState()
        try:
            data: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"{self.path}: invalid JSON ({exc})") from exc
        return from_json(data)

    def save(self, state: UpdaterState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(to_json(state), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        atomic_write(self.path, text.encode("utf-8"))


class BusyError(Exception):
    """다른 Updater 프로세스가 상태 디렉터리를 쓰고 있다."""


def _try_lock(handle: IO[bytes]) -> bool:
    if sys.platform == "win32":
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def process_lock(state_dir: Path) -> Generator[None]:
    """비차단 배타 잠금. 잡지 못하면 `BusyError`. 프로세스가 죽으면 OS가 푼다."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK_FILE).open("a+b") as handle:
        if not _try_lock(handle):
            raise BusyError(f"another updater holds {state_dir / LOCK_FILE}")
        yield
