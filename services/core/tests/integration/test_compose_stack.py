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


def _collector_clock(container: str) -> tuple[float, float, float]:
    """collector 컨테이너 안의 (realtime, monotonic) 초와 이를 읽은 `docker exec` 왕복(호스트 초).

    collector는 poll 주기를 monotonic으로 세고 샘플 시각은 realtime이다. Docker Desktop(WSL2)
    VM은 monotonic이 호스트보다 수 % 느리고 Hyper-V TimeSync가 realtime을 몇 초씩 계단 보정할
    수 있어(M3a-3 W1 실측: monotonic -7%, ~32s마다 +2.3s step), 호스트 벽시계로 기대 행 수를
    세면 실제 poll 수보다 많아진다. 두 값은 같은 프로세스에서 연달아 읽어 서로 대응한다.
    """
    probe = "import time; print(time.time(), time.monotonic())"
    started = time.monotonic()
    out = subprocess.run(
        ["docker", "exec", container, "python", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return float(out[0]), float(out[1]), time.monotonic() - started


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


def _stable_replay(timeout_s: float) -> int:
    """replay 카운터를 1s 간격으로 읽어 세 번 연속(비교 2쌍) 같을 때까지 기다린다.

    replay는 batch 여러 개로 오고, batch 사이가 1s를 넘을 수 있다(재접속이 겹치는 등).
    """
    deadline = time.monotonic() + timeout_s
    last = _int_field(_ingest(), "replay")
    same = 0
    while time.monotonic() < deadline:
        time.sleep(1.0)
        current = _int_field(_ingest(), "replay")
        same = same + 1 if current == last else 0
        if same >= 2:
            return current
        last = current
    raise AssertionError(f"replay counter still moving after {timeout_s}s: {last}")


@pytest.mark.compose
def test_network_disconnect_replay(stack: Stack, tmp_path: Path) -> None:
    """collector를 core 쪽 네트워크(default)에서 30s 떼었다 붙인다(장비 field 네트워크는 유지).

    창은 분리 + EDGE_S 시점(ta, ma)과 재연결 - EDGE_S 시점(tb, mb)에 collector 안에서 직접 읽은
    (realtime, monotonic) 쌍으로 정한다: 창 [ta, tb)는 샘플 시각과 같은 collector realtime이고,
    그 안의 poll은 collector가 주기를 세는 monotonic 구간 [ma, mb)에 정확히 대응한다 — 게스트
    realtime이 창 안팎 어디서 앞으로 계단 보정되든 판정이 흔들리지 않는다. realtime이
    **뒤로** step 하면 같은 realtime 창에 monotonic이 더 들어가므로 이 대응은 가정 밖이다(W1
    실측은 앞으로만).

    ① 창 안 태그별 이력 행 ≥ 기대 poll 수 (mb - ma) / POLL_S의 0.9배.
    ② 대량 중복 없음 — 창 안 태그별 행 ≤ (mb - ma) / POLL_S + 1(창 안에서 실제로 일어난 poll 수).
       같은 샘플의 live·replay 중복은 같은 (tag_id, time)이라 UNIQUE가 구조적으로 흡수하고, 다른
       시각으로 두 번 기록되면 이 상한을 넘는다. 예전 "같은 태그 행 간격 ≥ 0.5 poll"은 realtime
       차라서 역 step에 거짓 실패할 수 있어 쓰지 않는다(W1 §4).
    ③ `/readyz.acquisition.ingest.replay` 증가.
    ④ 분리 구간 시각이 현재값·이벤트에 안 박힘 — 현재값 조회 API가 M3b(D8)라 간접 증명:
       (주) 분리 동안 `/readyz.acquisition.ingest.live`(현재값으로 간 샘플 수)가 늘지 않는다 —
       분리 구간 안에서 창 샘플의 live 유입 0.
       (주2) 분리부터 재연결 뒤 replay 안정까지 `ingest.stale`(live로 왔지만 live_max_age 5s보다
       오래돼 현재값에서 뺀 샘플)이 늘지 않는다 — 창 샘플 중 5s보다 오래된 것은 live 경로로 오지
       않았다. 창 끝 EDGE_S 안의 샘플이 재연결 지연까지 합쳐 5s 안에 live로 오면 아직 젊어서
       이 테스트로는 구별하지 못한다(남은 한계). (a) 보조: replay 증가분 ≥ 창 안 이력 행 수 합.
       (b) 재연결 뒤 tags_good == 100. (c) replay는 TagStore·`tag.changed`를 건드리지 않는다는
       단위 근거(test_session `test_live_replay_and_stale_samples`·
       `test_replay_through_recorder_is_raw_and_emits_no_events`).
    """
    _apply_sim_project(stack, tmp_path)
    _wait_acquisition(lambda a: a.get("tags_good") == SIM_TAGS, 30)
    container = compose(stack.compose_env, "ps", "-q", "collector").stdout.strip()
    assert container, "collector container not running"
    network = f"{PROJECT}_default"
    ingest_before = _ingest()
    replay_before = _int_field(ingest_before, "replay")
    stale_before = _int_field(ingest_before, "stale")

    subprocess.run(["docker", "network", "disconnect", network, container], check=True)
    try:
        # 기준 시점 = 분리 + EDGE_S. disconnect 뒤 core에 새 패킷은 닿지 않고(core는 default
        # 네트워크에만 있다), 그 직전에 보낸 batch는 이미 core 소켓 버퍼에 있거나 사라진다 —
        # 버퍼에 있던 batch는 수 ms 안에 처리되므로 2s 뒤의 live 스냅샷은 그 유입을 포함한다.
        # core의 세션 끊김 감지(session_silence_s 15s)는 세션 정리일 뿐 새 live 유입이 아니다.
        time.sleep(EDGE_S)
        live_a = _int_field(_ingest(), "live")
        ta, ma, rtt_a = _collector_clock(container)
        time.sleep(DISCONNECT_S - 2 * EDGE_S)
        tb, mb, rtt_b = _collector_clock(container)
        live_b = _int_field(_ingest(), "live")
        time.sleep(EDGE_S)
    finally:
        subprocess.run(["docker", "network", "connect", network, container], check=True)
    print(f"REPLAY docker exec rtt a={rtt_a:.3f}s b={rtt_b:.3f}s")
    # 왕복이 poll 주기보다 길면 live 스냅샷과 창 경계 사이에 poll이 끼어 판정 근거가 흐려진다.
    assert max(rtt_a, rtt_b) <= POLL_S, (
        f"판정 불가(환경): collector clock probe round trip {rtt_a:.3f}s/{rtt_b:.3f}s > {POLL_S}s"
    )

    # 재연결 → replay(링에 남은 분리 구간) → plan → live. replay가 다 오고 버퍼가 모두 쓰일 때까지.
    _wait(_collector_ok, 60, "collector session after reconnect")
    _wait_acquisition(lambda a: a.get("tags_good") == SIM_TAGS, 30)  # ④(b)
    _wait(lambda: _int_field(_ingest(), "replay") > replay_before, 30, "replayed samples")
    replay_after = _stable_replay(30)
    stale_at_replay = _int_field(_ingest(), "stale") - stale_before
    _wait(lambda: _int_field(_section(_readyz(), "historian"), "buffer_rows") == 0, 30, "flush")
    replayed = replay_after - replay_before
    stale_delta = _int_field(_ingest(), "stale") - stale_before

    historian = _section(_readyz(), "historian")
    assert historian["db_ok"] is True
    assert historian["dropped_total"] == 0
    assert historian["rejected_total"] == 0

    counts = asyncio.run(_gap_rows(stack.dsn("pureiron"), stack.password, (ta, tb)))
    expected = (mb - ma) / POLL_S
    upper = expected + 1
    print(
        f"REPLAY rows/tag min={min(counts)} max={max(counts)} expected={expected:.2f} "
        f"ratio={min(counts) / expected:.3f} upper={upper:.2f} "
        f"window_real={tb - ta:.2f} window_mono={mb - ma:.2f} "
        f"replayed={replayed} gap_rows={sum(counts)} live_delta={live_b - live_a} "
        f"stale_at_replay={stale_at_replay} stale_delta={stale_delta}"
    )
    assert len(counts) == SIM_TAGS
    assert min(counts) >= 0.9 * expected, (min(counts), expected)  # ①
    assert max(counts) <= upper, (max(counts), upper)  # ②
    assert live_b == live_a, (live_a, live_b)  # ④(주)
    assert stale_at_replay == 0, stale_at_replay  # ④(주2)
    assert replayed >= sum(counts), (replayed, sum(counts))  # ③·④(a)
