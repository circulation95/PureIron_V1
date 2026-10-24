"""compose 종료 기준(M3c-2): 스위치 켠 core + 시뮬레이터 메모리 영역 쓰기 선언으로 setpoint
CONFIRMED(float32 반올림) + WS `tag_update`로 새 값 관찰 + 감사 2행(SENDING·CONFIRMED), 무시
주소(holding 198) MISMATCH, 예외 주소(holding 199) FAILED, permissive coil 거짓 409 +
`tag.write_rejected`(참이면 명령 WRITTEN), 범위 밖 409, edit-lock 409, collector 정지 409,
plan `writable`이 선언 태그만 참, 스위치 끈 core → `targets.enabled=false`·409·plan `writable`
전부 false.

스위치는 core env(`PUREIRON_WRITES_ENABLED`)라 core 컨테이너를 그 값으로 다시 만든다(`compose up -d
core`). 이 모듈의 compose 호출은 전부 같은 env로 한다 — 다른 env로 `up`하면 compose가 core를 다시
만든다. 끝나면 스위치를 끈 core로 되돌린다(다른 모듈은 `stack.compose_env` 그대로 쓴다). plan은
core 컨테이너 안에서 임시 gRPC 세션(Hello → 첫 plan)으로 읽는다.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import asyncpg
import pytest
from websockets.sync.client import ClientConnection, connect

from .conftest import (
    CORE_API,
    CORE_PORT,
    SIM_ALARMS,
    SIM_TAGS,
    Stack,
    compose,
    core_cli,
    ensure_user,
    holding_tag,
    login_token,
    modbus_device,
    wait_http,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.compose]

type Json = dict[str, object]

WS_URL = f"ws://127.0.0.1:{CORE_PORT}/api/v1/ws"
WRITER = "it-writer"
SP_FLOAT = "sim/sp_float"  # holding 100-101 float32, 메모리
SP_IGNORED = "sim/sp_ignored"  # holding 198 — 시뮬레이터가 쓰기를 무시(readback 불일치)
SP_REJECTED = "sim/sp_rejected"  # holding 199 — 시뮬레이터가 예외 응답(ILLEGAL DATA VALUE)
START = "sim/start"  # coil 10 명령
REMOTE_OK = "sim/remote_ok"  # coil 20 허가 조건(시험이 직접 바꾼다)
REMOTE_OK_COIL = 20
WRITE_PATHS = (SP_FLOAT, SP_IGNORED, SP_REJECTED, START)
FLOAT32_42_7 = 42.70000076293945  # float32로 쓴 42.7(가장 가까운 float32)
READY_WITHIN_S = 90.0

PLAN_PROBE = """
import asyncio, json
from grpc import aio
from pureiron_core._gen import collector_pb2 as pb, collector_pb2_grpc as pb_grpc

async def main():
    async with aio.insecure_channel("127.0.0.1:50051") as channel:
        async def requests():
            yield pb.CollectorMsg(hello=pb.Hello(collector_version="it-plan-probe"))
            await asyncio.sleep(60)
        call = pb_grpc.AcquisitionStub(channel).Session(requests())
        while True:
            message = await asyncio.wait_for(call.read(), 20)
            if message.WhichOneof("msg") == "plan":
                tags = {str(t.tag_id): t.writable for t in message.plan.tags}
                print(json.dumps({"plan_rev": message.plan.plan_rev, "writable": tags}))
                call.cancel()
                return

asyncio.run(main())
"""


@dataclass(frozen=True, slots=True)
class Writer:
    stack: Stack
    env: Mapping[str, str]
    token: str
    tag_ids: Mapping[str, int]


def _coil_tag(path: str, address: int, write: Json | None = None) -> Json:
    tag: Json = {
        "path": path,
        "device": "sim",
        "modbus": {"register_type": "coil", "address": address, "data_type": "bool"},
    }
    if write is not None:
        tag["write"] = write
    return tag


def _setpoint(path: str, address: int, data_type: str, maximum: float) -> Json:
    return {
        **holding_tag(path, "sim", address, data_type),
        "write": {"kind": "setpoint", "min": 0, "max": maximum},
    }


def _apply(stack: Stack, root: Path) -> None:
    """시뮬레이터 사인파 태그·알람(다른 모듈과 같은 정의) + 메모리 영역 쓰기 선언."""
    tags = [holding_tag(f"sim/r{i:03d}", "sim", i) for i in range(SIM_TAGS)] + [
        _setpoint(SP_FLOAT, 100, "float32", 500),
        _setpoint(SP_IGNORED, 198, "uint16", 1000),
        _setpoint(SP_REJECTED, 199, "uint16", 1000),
        _coil_tag(START, 10, {"kind": "command", "values": [True], "permissive": [REMOTE_OK]}),
        _coil_tag(REMOTE_OK, REMOTE_OK_COIL),
    ]
    project = write_project(
        root, {"sim": modbus_device("sim", "simulator", 5020)}, {"sim": tags}, alarms=SIM_ALARMS
    )
    applied = core_cli(stack.cli_env("pureiron"), "apply-definitions", str(project), "--json")
    assert applied.returncode == 0, applied.stdout + applied.stderr


def _restart_core(env: Mapping[str, str], *, enabled: bool) -> None:
    compose(env, "up", "-d", "--wait", "core")
    deadline = time.monotonic() + READY_WITHIN_S
    while True:
        ready = cast("Json", json.loads(wait_http(f"{CORE_API}/readyz", timeout_s=READY_WITHIN_S)))
        if cast("Json", ready["writes"])["enabled"] is enabled:
            return
        assert time.monotonic() < deadline, ready
        time.sleep(1)


def _request(method: str, path: str, token: str, body: Json | None = None) -> tuple[int, Json]:
    request = urllib.request.Request(
        f"{CORE_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, cast("Json", json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        return exc.code, cast("Json", json.loads(exc.read() or b"{}"))


def _prepare(writer: Writer, path: str, value: float | bool) -> tuple[int, Json]:
    return _request(
        "POST", "/writes", writer.token, {"tag_path": path, "value": value, "reason": "it"}
    )


def _confirm(writer: Writer, write_id: object) -> tuple[int, Json]:
    return _request("POST", f"/writes/{write_id}/confirm", writer.token)


def _failed(body: Json) -> set[str]:
    checks = cast("list[Json]", body["checks"])
    return {str(c["name"]) for c in checks if not c["ok"]}


def _wait_writable(writer: Writer) -> None:
    """수집·plan 보고가 끝나 유효한 쓰기가 검사를 통과할 때까지(준비만 하고 확인하지 않는다)."""
    deadline = time.monotonic() + READY_WITHIN_S
    while True:
        status, body = _prepare(writer, SP_FLOAT, 1.0)
        if status == 200:
            return
        assert time.monotonic() < deadline, (status, body)
        time.sleep(1)


def _set_remote_ok(writer: Writer, value: bool) -> None:
    script = (
        "from pymodbus.client import ModbusTcpClient\n"
        "c = ModbusTcpClient('127.0.0.1', port=5020)\n"
        "assert c.connect()\n"
        f"r = c.write_coil({REMOTE_OK_COIL}, {value}, device_id=1)\n"
        "assert not r.isError(), r\n"
        "c.close()\n"
    )
    written = compose(writer.env, "exec", "-T", "simulator", "python", "-", stdin=script)
    assert written.returncode == 0, written.stdout + written.stderr
    deadline = time.monotonic() + 15
    while True:
        status, body = _request("GET", "/tags", writer.token)
        assert status == 200, body
        tag = next(t for t in cast("list[Json]", body["tags"]) if t["path"] == REMOTE_OK)
        if (tag["value"], tag["quality"]) == (value, "GOOD"):
            return
        assert time.monotonic() < deadline, tag
        time.sleep(0.5)


def _plan(env: Mapping[str, str]) -> Json:
    probed = compose(env, "exec", "-T", "core", "python", "-", stdin=PLAN_PROBE)
    assert probed.returncode == 0, probed.stdout + probed.stderr
    return cast("Json", json.loads(probed.stdout.strip().splitlines()[-1]))


async def _audit(stack: Stack, write_id: str) -> list[Json]:
    conn = await asyncpg.connect(stack.dsn("pureiron"), password=stack.password)
    try:
        rows = await conn.fetch(
            "SELECT action, username, target, detail::text AS detail FROM audit_log"
            " WHERE detail->>'write_id' = $1 ORDER BY id",
            write_id,
        )
    finally:
        await conn.close()
    return [{**dict(r), "detail": json.loads(str(r["detail"]))} for r in rows]


async def _boot_rows(stack: Stack) -> int:
    conn = await asyncpg.connect(stack.dsn("pureiron"), password=stack.password)
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'writes.enabled_at_boot'"
        )
    finally:
        await conn.close()
    return int(count)


@pytest.fixture(scope="module")
def writer(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> Generator[Writer]:
    env = {**stack.compose_env, "PUREIRON_WRITES_ENABLED": "true"}
    wait_http(f"{CORE_API}/readyz", timeout_s=120)
    _apply(stack, tmp_path_factory.mktemp("writes") / "project")
    _restart_core(env, enabled=True)
    ensure_user(stack, WRITER, "operator")
    token = login_token(WRITER)
    status, body = _request("GET", "/writes/targets", token)
    assert status == 200, body
    targets = cast("list[Json]", body["targets"])
    built = Writer(
        stack, env, token, {str(t["path"]): int(cast("int", t["tag_id"])) for t in targets}
    )
    try:
        _set_remote_ok(built, True)
        _wait_writable(built)
        yield built
    finally:
        # 스위치를 끈 core + collector로 되돌린다(정지 시험이 실패했어도). 기본 env로 `up`하면
        # compose가 core를 그 값으로 다시 만든다.
        compose(stack.compose_env, "up", "-d", "collector")
        _restart_core(stack.compose_env, enabled=False)


def _subscribe(writer: Writer, tag_id: int) -> ClientConnection:
    ws = connect(WS_URL, additional_headers={"Cookie": f"pureiron_session={writer.token}"})
    hello = json.loads(ws.recv(timeout=5))
    assert hello["type"] == "hello", hello
    ws.send(json.dumps({"type": "subscribe", "tag_ids": [tag_id]}))
    return ws


def _wait_tag_update(ws: ClientConnection, tag_id: int, value: float, timeout_s: float) -> Json:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            message = cast(
                "Json", json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            )
        except TimeoutError:
            break
        if message["type"] != "tag_update":
            continue
        for item in cast("list[Json]", message["values"]):
            if item["tag_id"] == tag_id and item["value"] == value:
                return item
    raise AssertionError(f"no tag_update with value {value} for tag {tag_id}")


def test_targets_and_plan_mark_only_declared_tags(writer: Writer) -> None:
    status, body = _request("GET", "/writes/targets", writer.token)
    assert status == 200
    assert body["enabled"] is True
    assert sorted(writer.tag_ids) == sorted(WRITE_PATHS)
    plan = _plan(writer.env)
    writable = cast("dict[str, bool]", plan["writable"])
    assert {int(k) for k, v in writable.items() if v} == set(writer.tag_ids.values())
    assert len(writable) == SIM_TAGS + len(WRITE_PATHS) + 1
    assert asyncio.run(_boot_rows(writer.stack)) >= 1  # 스위치를 켠 기동이 감사에 남는다


def test_setpoint_confirmed_seen_on_ws_and_audited_twice(writer: Writer) -> None:
    tag_id = writer.tag_ids[SP_FLOAT]
    with _subscribe(writer, tag_id) as ws:
        status, prepared = _prepare(writer, SP_FLOAT, 42.7)
        assert status == 200, prepared
        assert set(_failed(prepared)) == set()
        status, result = _confirm(writer, prepared["write_id"])
        assert status == 200, result
        assert (result["outcome"], result["readback"]) == ("CONFIRMED", FLOAT32_42_7), result
        update = _wait_tag_update(ws, tag_id, FLOAT32_42_7, timeout_s=10)
        assert update["quality"] == "GOOD"
    rows = asyncio.run(_audit(writer.stack, str(prepared["write_id"])))
    assert [(r["action"], cast("Json", r["detail"])["outcome"]) for r in rows] == [
        ("tag.write", "SENDING"),
        ("tag.write", "CONFIRMED"),
    ]
    sending, confirmed = (cast("Json", r["detail"]) for r in rows)
    assert (rows[0]["username"], rows[0]["target"]) == (WRITER, SP_FLOAT)
    assert (sending["target"], sending["origin"], sending["approved_by"]) == (42.7, "human", WRITER)
    assert cast("Json", sending["before"])["quality"] == "GOOD"
    assert confirmed["readback"] == FLOAT32_42_7
    assert sending["request_id"] == confirmed["request_id"]
    # 1회용: 같은 id 두 번째 confirm은 보내지 않는다
    assert _confirm(writer, prepared["write_id"])[0] == 404


@pytest.mark.parametrize(
    ("path", "value", "outcome"),
    [(SP_IGNORED, 123, "MISMATCH"), (SP_REJECTED, 5, "FAILED")],
    ids=["ignored-198", "exception-199"],
)
def test_simulator_reject_addresses(writer: Writer, path: str, value: int, outcome: str) -> None:
    status, prepared = _prepare(writer, path, value)
    assert status == 200, prepared
    status, result = _confirm(writer, prepared["write_id"])
    assert status == 200, result
    assert result["outcome"] == outcome, result
    rows = asyncio.run(_audit(writer.stack, str(prepared["write_id"])))
    assert [cast("Json", r["detail"])["outcome"] for r in rows] == ["SENDING", outcome]


def test_permissive_false_is_rejected_and_audited_then_command_written(writer: Writer) -> None:
    _set_remote_ok(writer, False)
    try:
        status, body = _prepare(writer, START, True)
        assert status == 409, body
        assert _failed(body) == {"permissive"}
    finally:
        _set_remote_ok(writer, True)
    status, prepared = _prepare(writer, START, True)
    assert status == 200, prepared
    status, result = _confirm(writer, prepared["write_id"])
    assert (status, result["outcome"]) == (200, "WRITTEN"), result
    rejected = asyncio.run(_rejected_rows(writer.stack, START))
    assert rejected, "tag.write_rejected row missing"
    detail = cast("Json", rejected[-1]["detail"])
    assert (detail["stage"], detail["failed"], detail["outcome"]) == (
        "prepare",
        ["permissive"],
        "REJECTED",
    )


async def _rejected_rows(stack: Stack, path: str) -> list[Json]:
    conn = await asyncpg.connect(stack.dsn("pureiron"), password=stack.password)
    try:
        rows = await conn.fetch(
            "SELECT username, detail::text AS detail FROM audit_log"
            " WHERE action = 'tag.write_rejected' AND target = $1 ORDER BY id",
            path,
        )
    finally:
        await conn.close()
    return [{"username": r["username"], "detail": json.loads(str(r["detail"]))} for r in rows]


def test_out_of_range_is_rejected(writer: Writer) -> None:
    status, body = _prepare(writer, SP_FLOAT, 9999.0)
    assert status == 409, body
    assert _failed(body) == {"value"}


def test_edit_lock_blocks_writes(writer: Writer) -> None:
    token = writer.stack.updater_token
    status, _ = _request(
        "POST", "/project/edit-lock", token, {"owner": "it-writes", "generation": 1}
    )
    assert status == 200
    try:
        status, body = _prepare(writer, SP_FLOAT, 10.0)
        assert status == 409, body
        assert _failed(body) == {"no_edit_lock"}
    finally:
        assert _request("DELETE", "/project/edit-lock?owner=it-writes", token)[0] == 204


def test_collector_stopped_blocks_writes(writer: Writer) -> None:
    compose(writer.env, "stop", "collector")
    try:
        deadline = time.monotonic() + 30
        while True:
            status, body = _prepare(writer, SP_FLOAT, 10.0)
            if status == 409 and "single_collector_session" in _failed(body):
                break
            assert time.monotonic() < deadline, (status, body)
            time.sleep(0.5)
    finally:
        compose(writer.env, "up", "-d", "collector")
    _wait_writable(writer)


def test_switched_off_core_rejects_and_plan_is_read_only(writer: Writer) -> None:
    """마지막 시험: 스위치를 끈 core로 되돌린다(fixture 정리도 같은 상태로 끝난다)."""
    _restart_core(writer.stack.compose_env, enabled=False)
    status, body = _request("GET", "/writes/targets", writer.token)
    assert status == 200
    assert body["enabled"] is False
    assert len(cast("list[Json]", body["targets"])) == len(WRITE_PATHS)  # 선언은 보인다
    status, rejected = _prepare(writer, SP_FLOAT, 10.0)
    assert status == 409, rejected
    assert "writes_enabled" in _failed(rejected)
    plan = _plan(writer.stack.compose_env)
    assert not any(cast("dict[str, bool]", plan["writable"]).values())
