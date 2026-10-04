"""상태 파일 왕복·형식 오류·원자적 쓰기·프로세스 잠금, 적용 창 판정."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pureiron_bundle.manifest import Json
from pureiron_bundle.trust import TrustState
from pureiron_bundle.verify import AttemptResult
from pureiron_updater.state import (
    Attempt,
    BusyError,
    Phase,
    StateError,
    StateStore,
    UpdaterState,
    atomic_write,
    from_json,
    process_lock,
    skip_bundle,
    to_json,
)
from pureiron_updater.window import WindowError, window_open

T = datetime(2026, 10, 5, 1, 2, 3, tzinfo=UTC)


def full_state() -> UpdaterState:
    attempt = Attempt(
        phase=Phase.ROLLING_BACK,
        bundle="/var/lib/pureiron/bundles/g7.pib",
        bundle_sha256="sha256:" + "a" * 64,
        extract_dir="/var/lib/pureiron/bundles/g7.pib.d",
        started_at=T,
        generation=7,
        from_generation=6,
        migrate_to=3,
        trust_after=TrustState(2, T),
        baseline_good_ratio=0.97,
        lock_held=True,
        failed_phase=Phase.CHECKING,
        code="E_HEALTH",
        detail="not healthy",
        rollback_tries=1,
        verify_s=1.5,
        load_s=2.25,
        timeline={"fetched": T, "rolling_back": T},
    )
    return UpdaterState(
        applied_gen=6,
        last_attempted_gen=7,
        last_result=AttemptResult.PENDING,
        schema_epoch=3,
        trust=TrustState(2, T),
        attempt=attempt,
        skipped_bundles=("sha256:" + "b" * 64,),
        alarm=None,
    )


def test_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    assert store.load() == UpdaterState()
    state = full_state()
    store.save(state)
    assert store.load() == state
    assert from_json(to_json(state)) == state
    assert not (tmp_path / "updater.json.tmp").exists()


@pytest.mark.parametrize(
    "text",
    [
        "{",
        '{"format": 2}',
        '{"format": 1, "last_result": "maybe"}',
        '{"format": 1, "applied_gen": "3"}',
        '{"format": 1, "attempt": {"phase": "dancing", "bundle": "b", "bundle_sha256": "s",'
        ' "extract_dir": "d", "started_at": "2026-10-05T00:00:00Z"}}',
        '{"format": 1, "skipped_bundles": [1]}',
    ],
)
def test_corrupt_state_is_an_error_not_a_reset(tmp_path: Path, text: str) -> None:
    (tmp_path / "updater.json").write_text(text, encoding="utf-8")
    with pytest.raises(StateError):
        StateStore(tmp_path).load()


def test_atomic_write_replaces_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    atomic_write(path, b"old")
    atomic_write(path, b"new")
    assert path.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.json"]


def test_skip_bundle_keeps_recent_unique() -> None:
    state = UpdaterState()
    for i in range(25):
        state = skip_bundle(state, f"s{i}")
    state = skip_bundle(state, "s10")
    assert len(state.skipped_bundles) == 20
    assert state.skipped_bundles[-1] == "s10"
    assert state.skipped_bundles.count("s10") == 1


def test_process_lock_is_exclusive(tmp_path: Path) -> None:
    with process_lock(tmp_path), pytest.raises(BusyError), process_lock(tmp_path):
        pass
    with process_lock(tmp_path):
        pass


RECURRING: Json = {
    "recurring": {"tz": "Asia/Seoul", "days": ["sat"], "start": "22:00", "end": "02:00"}
}


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-10-10T12:59:00+00:00", False),  # 토 21:59 KST
        ("2026-10-10T13:00:00+00:00", True),  # 토 22:00
        ("2026-10-10T16:30:00+00:00", True),  # 일 01:30(토 시작 창이 자정을 넘김)
        ("2026-10-10T17:00:00+00:00", False),  # 일 02:00 닫힘
        ("2026-10-11T13:30:00+00:00", False),  # 일 22:30 — 일요일은 창이 없다
    ],
)
def test_recurring_window_crosses_midnight(utc: str, expected: bool) -> None:
    assert window_open(RECURRING, datetime.fromisoformat(utc)) is expected


def test_once_window_is_half_open() -> None:
    window: Json = {
        "once": {"start": "2026-10-05T10:00:00+09:00", "end": "2026-10-05T11:00:00+09:00"}
    }
    assert window_open(window, datetime(2026, 10, 5, 1, 0, tzinfo=UTC))
    assert not window_open(window, datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    assert not window_open(window, datetime(2026, 10, 5, 0, 59, tzinfo=UTC))


def test_unknown_time_zone_is_rejected() -> None:
    bad: Json = {
        "recurring": {"tz": "Mars/Olympus", "days": ["mon"], "start": "01:00", "end": "02:00"}
    }
    with pytest.raises(WindowError):
        window_open(bad, T)
