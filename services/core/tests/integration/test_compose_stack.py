"""compose 수직 경로: site-db → migrate up → core·collector·web → readyz 200 (US-005).

M3a-2: 시뮬레이터 100태그 apply → 10s 내 100% GOOD, collector kill → 태그 BAD·장비 DISCONNECTED.
M3a-3: collector를 core 네트워크에서 30s 떼었다 붙이면 분리 구간 샘플이 replay로 이력에 들어오고
중복이 없다(장비 쪽 field 네트워크는 유지 — `site.dev.yml`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

import asyncpg
import pytest

from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database
from pureiron_core.project.ports import EditLockConflictError
from pureiron_core.project.stub import PgProjectStateStore

from .conftest import (
    CORE_PORT,
    MIGRATIONS,
    PROJECT,
    WEB_PORT,
    SiteDb,
    Stack,
    compose,
    core_cli,
    holding_tag,
    modbus_device,
    wait_http,
    write_project,
)

pytestmark = pytest.mark.integration

CORE = f"http://127.0.0.1:{CORE_PORT}/api/v1"
WEB = f"http://127.0.0.1:{WEB_PORT}"


def _request(method: str, url: str, token: str, body: dict[str, object] | None = None) -> int:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        status: int = response.status
        return status


@pytest.mark.compose
def test_readyz_200_through_core_and_web(stack: Stack) -> None:
    ready = json.loads(wait_http(f"{CORE}/readyz", timeout_s=120))
    assert ready["status"] == "ready"
    assert ready["checks"]["db"]["ok"] is True
    assert (
        ready["checks"]["schema_envelope"]["cur"] == migrate.load_catalog(MIGRATIONS).app_expected
    )
    assert ready["checks"]["collector"]["ok"] is True, "collector must hold a gRPC session"

    via_web = json.loads(wait_http(f"{WEB}/api/v1/readyz", timeout_s=60))
    assert via_web["status"] == "ready"
    index = wait_http(f"{WEB}/", timeout_s=30).decode()
    assert '<div id="root">' in index


@pytest.mark.compose
def test_edit_lock_with_updater_secret(stack: Stack) -> None:
    wait_http(f"{CORE}/readyz", timeout_s=120)
    lock = f"{CORE}/project/edit-lock"
    assert _request("POST", lock, stack.updater_token, {"owner": "updater", "generation": 1}) == 200
    project = json.loads(wait_http(f"{CORE}/project"))
    assert project["edit_locked"] is True
    assert _request("DELETE", f"{lock}?owner=updater", stack.updater_token) == 204


async def test_edit_lock_race_has_single_winner(site_db: SiteDb) -> None:
    db = Database(site_db.dsn, password=site_db.password)
    store = PgProjectStateStore(db)
    try:
        await store.release_edit_lock("a")
        await store.release_edit_lock("b")
        results = await asyncio.gather(
            store.acquire_edit_lock("a", 1),
            store.acquire_edit_lock("b", 1),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, int)]
        losers = [r for r in results if isinstance(r, EditLockConflictError)]
        assert len(winners) == 1
        assert len(losers) == 1
        owner = (await store.get()).edit_lock_owner
        assert owner in {"a", "b"}
        assert await store.acquire_edit_lock(owner, 1) == winners[0]  # 같은 owner·gen 재요청은 멱등
        await store.release_edit_lock(owner)
        assert (await store.get()).edit_lock_owner is None
    finally:
        await db.close()


@pytest.mark.fresh_db
async def test_apply_records_base_site_rev_and_restore_roundtrip(site_db: SiteDb) -> None:
    db = Database(site_db.dsn, password=site_db.password)
    store = PgProjectStateStore(db)
    try:
        conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
        try:
            await conn.execute("UPDATE project_state SET site_rev = 5")
        finally:
            await conn.close()
        applied = await store.apply(project_id="p", version="v1", content_hash="sha256:" + "0" * 64)
        assert (applied.site_rev, applied.base_site_rev) == (5, 5)
        assert applied.applied_at is not None
        snapshot = applied.to_snapshot()

        await store.apply(project_id="p", version="v2", content_hash="sha256:" + "1" * 64)
        restored = await store.restore(snapshot)
        assert restored.version == "v1"
        assert restored.to_snapshot() == snapshot
    finally:
        await db.close()


SIM_TAGS = 100
GOOD_WITHIN_S = 10.0


def _readyz() -> dict[str, object]:
    """준비 여부와 무관하게(503 포함) readyz 본문."""
    try:
        with urllib.request.urlopen(f"{CORE}/readyz", timeout=3) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    data: dict[str, object] = json.loads(body)
    return data


def _wait_acquisition(
    predicate: Callable[[dict[str, object]], bool], timeout_s: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        acquisition = _readyz().get("acquisition")
        if isinstance(acquisition, dict):
            last = cast("dict[str, object]", acquisition)
            if predicate(last):
                return last
        time.sleep(0.25)
    raise AssertionError(f"acquisition did not reach the expected state in {timeout_s}s: {last}")


def _apply_sim_project(stack: Stack, tmp_path: Path) -> None:
    """시뮬레이터 holding 0..SIM_TAGS-1을 태그로 apply(테스트끼리 같은 정의 → 같은 id·rev)."""
    wait_http(f"{CORE}/readyz", timeout_s=120)
    root = write_project(
        tmp_path / "sim-project",
        {"sim": modbus_device("sim", "simulator", 5020)},
        {"sim": [holding_tag(f"sim/r{i:03d}", "sim", i) for i in range(SIM_TAGS)]},
    )
    applied = core_cli(stack.cli_env("pureiron"), "apply-definitions", str(root), "--json")
    assert applied.returncode == 0, applied.stdout + applied.stderr


@pytest.mark.compose
def test_sim_100_tags_good_then_collector_kill(stack: Stack, tmp_path: Path) -> None:
    _apply_sim_project(stack, tmp_path)

    good = _wait_acquisition(
        lambda a: a.get("tags_total") == SIM_TAGS and a.get("tags_good") == SIM_TAGS,
        GOOD_WITHIN_S,
    )
    assert good["good_ratio"] == 1.0
    _wait_acquisition(lambda a: a.get("devices_connected") == 1, 10)

    try:
        compose(stack.compose_env, "kill", "collector")
        down = _wait_acquisition(
            lambda a: a.get("tags_good") == 0 and a.get("devices_connected") == 0, 15
        )
        assert down["tags_total"] == SIM_TAGS
        assert down["good_ratio"] == 0.0
    finally:
        compose(stack.compose_env, "up", "-d", "collector")
        wait_http(f"{CORE}/readyz", timeout_s=60)  # 다른 compose 테스트를 위해 세션 복구


DISCONNECT_S = 30.0
POLL_S = 1.0  # 장비 기본 poll_ms
EDGE_S = 2.0  # 분리·재연결 경계(감지·재접속)를 빼고 보는 여유


def _section(data: dict[str, object], name: str) -> dict[str, object]:
    section = data.get(name)
    assert isinstance(section, dict), (name, data)
    return cast("dict[str, object]", section)


def _int_field(section: dict[str, object], key: str) -> int:
    value = section.get(key)
    assert isinstance(value, int), (key, value)
    return value


def _ingest() -> dict[str, object]:
    return _section(_section(_readyz(), "acquisition"), "ingest")


def _collector_ok() -> bool:
    return _section(_readyz(), "checks").get("collector") == {"ok": True, "detail": None}


def _wait(predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def _collector_clock(container: str) -> tuple[float, float]:
    """collector 컨테이너 안의 (realtime, monotonic) 초.

    collector는 poll 주기를 monotonic으로 세고 샘플 시각은 realtime이다. Docker Desktop(WSL2)
    VM은 monotonic이 호스트보다 수 % 느리고 Hyper-V TimeSync가 realtime을 몇 초씩 계단 보정할
    수 있어(M3a-3 W1 실측: monotonic -7%, ~32s마다 +2.3s step), 호스트 벽시계로 기대 행 수를
    세면 실제 poll 수보다 많아진다.
    """
    probe = "import time; print(time.time(), time.monotonic())"
    out = subprocess.run(
        ["docker", "exec", container, "python", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return float(out[0]), float(out[1])


async def _gap_rows(dsn: str, password: str, gap: tuple[float, float]) -> list[int]:
    """분리 구간 [gap) 태그별 이력 행 수(sim 태그 전부, 행이 없으면 0)."""
    conn = await asyncpg.connect(dsn, password=password)
    try:
        rows = await conn.fetch(
            "SELECT t.id, count(h.time) AS n FROM tags t"
            " LEFT JOIN tag_history h ON h.tag_id = t.id"
            "  AND h.time >= to_timestamp($1) AND h.time < to_timestamp($2)"
            " WHERE t.path LIKE 'sim/%' AND t.retired_at IS NULL GROUP BY t.id ORDER BY t.id",
            *gap,
        )
    finally:
        await conn.close()
    return [int(r["n"]) for r in rows]


@pytest.mark.compose
def test_network_disconnect_replay(stack: Stack, tmp_path: Path) -> None:
    """collector를 core 쪽 네트워크(default)에서 30s 떼었다 붙인다(장비 field 네트워크는 유지).

    ① 분리 구간 [t0+2, t1-2)에 태그별 이력 행 ≥ 기대 poll 수의 0.9배 — 창 경계는 샘플 시각과 같은
       collector realtime, 기대 poll 수는 collector가 실제로 주기를 세는 monotonic 경과(W1 판정식).
    ② 중복 0 — 시계와 무관한 상한: 창 안 태그별 행 수 ≤ 분리 동안 실제 poll 수(monotonic) + 1.
       같은 샘플의 live·replay 중복은 같은 (tag_id, time)이라 UNIQUE가 흡수하고, 다른 시각으로
       두 번 기록되면 이 상한을 넘는다. 예전의 "같은 태그 행 간격 ≥ 0.5 poll" 판정은 realtime
       차라서 게스트 시계가 뒤로 step 하는 환경에서 거짓 실패할 수 있어 쓰지 않는다(W1 §4).
    ③ `/readyz.acquisition.ingest.replay` 증가.
    ④ 분리 구간 시각이 현재값·이벤트에 안 박힘 — 현재값 조회 API가 M3b(D8)라 간접 증명:
       (a) replay 증가분 ≥ 창 안 이력 행 수 합(창 샘플이 replay 경로로만 들어왔다), (b) 재연결
       뒤 tags_good == 100, (c) replay는 TagStore·`tag.changed`를 건드리지 않는다는 단위 근거
       (test_session `test_live_replay_and_stale_samples`·
       `test_replay_through_recorder_is_raw_and_emits_no_events`).
    """
    _apply_sim_project(stack, tmp_path)
    _wait_acquisition(lambda a: a.get("tags_good") == SIM_TAGS, 30)
    container = compose(stack.compose_env, "ps", "-q", "collector").stdout.strip()
    assert container, "collector container not running"
    network = f"{PROJECT}_default"
    replay_before = _int_field(_ingest(), "replay")

    t0, mono0 = _collector_clock(container)
    subprocess.run(["docker", "network", "disconnect", network, container], check=True)
    try:
        time.sleep(DISCONNECT_S)
    finally:
        try:
            t1, mono1 = _collector_clock(container)
        finally:
            subprocess.run(["docker", "network", "connect", network, container], check=True)

    # 재연결 → replay(링에 남은 분리 구간) → plan → live. 버퍼가 모두 쓰일 때까지 기다린다.
    _wait(_collector_ok, 60, "collector session after reconnect")
    _wait_acquisition(lambda a: a.get("tags_good") == SIM_TAGS, 30)  # ④(b)
    _wait(lambda: _int_field(_ingest(), "replay") > replay_before, 30, "replayed samples")
    _wait(lambda: _int_field(_section(_readyz(), "historian"), "buffer_rows") == 0, 30, "flush")
    replayed = _int_field(_ingest(), "replay") - replay_before

    historian = _section(_readyz(), "historian")
    assert historian["db_ok"] is True
    assert historian["dropped_total"] == 0
    assert historian["rejected_total"] == 0

    gap = (t0 + EDGE_S, t1 - EDGE_S)
    counts = asyncio.run(_gap_rows(stack.dsn("pureiron"), stack.password, gap))
    expected = (mono1 - mono0 - 2 * EDGE_S) / POLL_S
    polls_upper = (mono1 - mono0) / POLL_S + 1
    print(
        f"REPLAY rows/tag min={min(counts)} max={max(counts)} expected={expected:.1f} "
        f"ratio={min(counts) / expected:.3f} upper={polls_upper:.1f} "
        f"real_span={t1 - t0:.2f} mono_span={mono1 - mono0:.2f} "
        f"replayed={replayed} gap_rows={sum(counts)}"
    )
    assert len(counts) == SIM_TAGS
    assert min(counts) >= 0.9 * expected, (min(counts), expected)  # ①
    assert max(counts) <= polls_upper, (max(counts), polls_upper)  # ②
    assert replayed >= sum(counts), (replayed, sum(counts))  # ③·④(a)
