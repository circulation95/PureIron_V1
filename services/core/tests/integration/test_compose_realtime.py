"""compose 종료 기준(M3b-2): 시뮬레이터 알람 → WS `alarm_event` ACTIVE_UNACKED가 조건 성립 후
≤ 2s → REST ACK → `alarm_journal` ACTIVE_ACKED·`audit_log` alarm.ack, `GET /history` 1m·1h,
`GET /devices` CONNECTED, collector kill → WS `device_update` DISCONNECTED. 실제 core 이미지
(uvicorn + websockets)로 `/api/v1/ws` 업그레이드가 되는 것도 함께 증명한다.

≤ 2s 판정 근거: 알람 이벤트의 `value_ts`는 전이를 만든 샘플 시각(collector가 poll한 순간,
Docker VM 벽시계) = 조건이 성립한 시각이다. 수신 시각은 호스트 벽시계라, 같은 순간의 core
컨테이너 벽시계를 `docker exec`로 읽어 차이(offset)를 더해 VM 시계로 옮긴다. 지연 = 수신(VM
시계) - value_ts. offset을 읽은 왕복 시간의 절반을 측정 불확실성으로 더 허용한다(왕복이 1s를
넘으면 판정 불가로 실패).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from typing import cast

import asyncpg
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .conftest import (
    CORE_API,
    CORE_PORT,
    SIM_ALARMS,
    Stack,
    api,
    apply_sim_project,
    compose,
    ensure_user,
    login_token,
    wait_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.compose]

WS_URL = f"ws://127.0.0.1:{CORE_PORT}/api/v1/ws"
ALARM_ID = cast("str", SIM_ALARMS[0]["id"])
OPERATOR = "it-operator"
ALARM_WITHIN_S = 2.0
# 사인파 주기 2π·10 ≈ 63s — 해제·재발생을 한 번씩 보려면 두 주기 남짓.
CYCLE_WAIT_S = 150.0

type Json = dict[str, object]


def _vm_offset_s(stack: Stack) -> tuple[float, float]:
    """(core 컨테이너 벽시계 - 호스트 벽시계, 왕복 초)."""
    container = compose(stack.compose_env, "ps", "-q", "core").stdout.strip()
    started = time.time()
    out = subprocess.run(
        ["docker", "exec", container, "python", "-c", "import time; print(time.time())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ended = time.time()
    return float(out) - (started + ended) / 2, ended - started


def _recv(ws: ClientConnection, timeout_s: float) -> Json | None:
    try:
        raw = ws.recv(timeout=timeout_s)
    except TimeoutError:
        return None
    return cast("Json", json.loads(raw))


def _alarm_state(token: str) -> str:
    status, body = api("GET", "/alarms", token)
    assert status == 200, body
    alarms = cast("list[Json]", body["alarms"])
    return next((str(a["state"]) for a in alarms if a["alarm_id"] == ALARM_ID), "NORMAL")


def _ws(token: str) -> ClientConnection:
    ws = connect(WS_URL, additional_headers={"Cookie": f"pureiron_session={token}"})
    hello = _recv(ws, 5)
    assert hello is not None
    assert (hello["type"], hello["username"], hello["role"]) == ("hello", OPERATOR, "operator")
    return ws


@pytest.fixture(scope="module")
def operator(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> str:
    apply_sim_project(stack, tmp_path_factory.mktemp("rt"))
    ensure_user(stack, OPERATOR, "operator")
    return login_token(OPERATOR)


def _wait_normal(token: str) -> None:
    """알람을 NORMAL로 돌려 둔다: 미확인이면 ACK(활성 ACK는 해제되면 바로 NORMAL)."""
    deadline = time.monotonic() + CYCLE_WAIT_S
    while (state := _alarm_state(token)) != "NORMAL":
        assert time.monotonic() < deadline, f"alarm stuck in {state}"
        if state in ("ACTIVE_UNACKED", "CLEARED_UNACKED"):
            api("POST", f"/alarms/{ALARM_ID}/ack", token)
        time.sleep(1)


def _wait_journal_flushed(timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        alarms = cast("Json", json.loads(wait_http(f"{CORE_API}/readyz", timeout_s=30))["alarms"])
        if alarms["journal_queue_rows"] == 0:
            return
        assert time.monotonic() < deadline, f"alarm journal not flushed: {alarms}"
        time.sleep(0.5)


async def _db_rows(stack: Stack, since: datetime) -> tuple[list[Json], list[Json]]:
    conn = await asyncpg.connect(stack.dsn("pureiron"), password=stack.password)
    try:
        journal = await conn.fetch(
            "SELECT from_state, to_state, username FROM alarm_journal"
            " WHERE alarm_id = $1 AND time >= $2 ORDER BY time",
            ALARM_ID,
            since,
        )
        audit = await conn.fetch(
            "SELECT action, username, target, detail::text AS detail FROM audit_log"
            " WHERE action = 'alarm.ack' AND time >= $1",
            since,
        )
    finally:
        await conn.close()
    return [dict(r) for r in journal], [dict(r) for r in audit]


def test_alarm_ws_within_2s_then_ack_journal_and_audit(stack: Stack, operator: str) -> None:
    _wait_normal(operator)
    offset_s, rtt_s = _vm_offset_s(stack)
    assert rtt_s <= 1.0, f"판정 불가(환경): docker exec round trip {rtt_s:.3f}s"
    since = datetime.now(UTC) + timedelta(seconds=offset_s) - timedelta(seconds=1)
    with _ws(operator) as ws:
        deadline = time.monotonic() + CYCLE_WAIT_S
        event: Json | None = None
        received_vm = 0.0
        while event is None and time.monotonic() < deadline:
            message = _recv(ws, 5)
            if (
                message is not None
                and message["type"] == "alarm_event"
                and message["alarm_id"] == ALARM_ID
                and message["state"] == "ACTIVE_UNACKED"
            ):
                received_vm = time.time() + offset_s
                event = message
        assert event is not None, "no ACTIVE_UNACKED alarm_event within one sine cycle"
        value_ts = datetime.fromisoformat(str(event["value_ts"])).timestamp()
        latency_s = received_vm - value_ts
        print(f"ALARM latency={latency_s:.3f}s rtt={rtt_s:.3f}s value={event['value']}")
        assert float(cast("float", event["value"])) >= 1000.0
        assert latency_s <= ALARM_WITHIN_S + rtt_s / 2, latency_s

        status, acked = api("POST", f"/alarms/{ALARM_ID}/ack", operator)
        assert status == 200, acked
        assert (acked["state"], acked["changed_by"]) in {
            ("ACTIVE_ACKED", OPERATOR),
            ("NORMAL", OPERATOR),  # 그 사이 해제됐으면 ACK가 곧 NORMAL
        }
        assert api("POST", f"/alarms/{ALARM_ID}/ack", operator)[0] in {409, 200}
    _wait_journal_flushed()  # 조건 변화 전이는 writer 큐(1s 주기)를 거쳐 쓰인다
    journal, audit = asyncio.run(_db_rows(stack, since))
    acks = [r for r in journal if r["username"] == OPERATOR]
    assert acks, journal
    assert acks[0]["from_state"] in {"ACTIVE_UNACKED", "CLEARED_UNACKED"}
    assert {"from_state": "NORMAL", "to_state": "ACTIVE_UNACKED", "username": None} in journal
    assert any(r["target"] == ALARM_ID and r["username"] == OPERATOR for r in audit), audit


def test_history_1m_1h_and_tags(operator: str) -> None:
    status, body = api("GET", "/tags", operator)
    assert status == 200
    tag = next(t for t in cast("list[Json]", body["tags"]) if t["path"] == "sim/r000")
    now = datetime.now(UTC)
    for resolution, span in (("1m", timedelta(minutes=30)), ("1h", timedelta(hours=2))):
        query = (
            f"/history?tag_id={tag['tag_id']}&resolution={resolution}"
            f"&start={(now - span).isoformat().replace('+00:00', 'Z')}"
            f"&end={(now + timedelta(minutes=2)).isoformat().replace('+00:00', 'Z')}"
        )
        status, history = api("GET", query, operator)
        assert status == 200, history
        assert history["resolution"] == resolution
        [series] = cast("list[Json]", history["series"])
        assert cast("list[Json]", series["buckets"]), (resolution, series)


def test_devices_connected_then_collector_kill_sends_device_update(
    stack: Stack, operator: str
) -> None:
    status, body = api("GET", "/devices", operator)
    assert status == 200
    [device] = cast("list[Json]", body["devices"])
    assert (device["device_id"], device["state"], device["connected"]) == ("sim", "CONNECTED", True)
    with _ws(operator) as ws:
        try:
            compose(stack.compose_env, "kill", "collector")
            deadline = time.monotonic() + 30
            update: Json | None = None
            while time.monotonic() < deadline and update is None:
                message = _recv(ws, 5)
                if message is not None and message["type"] == "device_update":
                    update = message
            assert update is not None, "no device_update after collector kill"
            assert (update["device_id"], update["state"]) == ("sim", "DISCONNECTED")
        finally:
            compose(stack.compose_env, "up", "-d", "collector")
            wait_http(f"{CORE_API}/readyz", timeout_s=60)


def test_ws_rejects_missing_session() -> None:
    with connect(WS_URL) as ws:
        ws.send(json.dumps({"type": "auth", "token": "not-a-session"}))
        with pytest.raises(ConnectionClosed) as excinfo:
            ws.recv(timeout=10)
    assert excinfo.value.rcvd is not None
    assert excinfo.value.rcvd.code == 4401


@pytest.mark.usefixtures("operator")
def test_readyz_reports_alarms_and_connections() -> None:
    ready = json.loads(wait_http(f"{CORE_API}/readyz", timeout_s=60))
    assert set(cast("Json", ready["alarms"])) == {
        "active_total",
        "unacked_total",
        "journal_queue_rows",
        "dropped_total",
    }
    assert isinstance(cast("Json", ready["realtime"])["connections"], int)
