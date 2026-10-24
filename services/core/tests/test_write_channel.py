"""acquisition `WriteChannel`(plan M3c D12): 세션 하나일 때만 `WriteRequest`를 보내고 결과를
request_id로 돌려준다. 시한 초과·세션 종료 → NO_RESPONSE, 모르는 status → INDETERMINATE, 늦은·모르는
결과는 무시, `write_path`는 세션이 마지막으로 보고한 live batch plan_rev(replay 제외)를 낸다. plan
`writable`은 PlanKeeper의 `writable`이 정한다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence

from grpc import aio

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.acquisition.ports import WriteCommand, WriteReply, WriteReplyStatus
from pureiron_core.acquisition.server import PlanKeeper
from pureiron_core.project.ports import Definitions
from pureiron_core.tags.ports import TagKey

from .conftest import sample_definitions
from .test_session import Core, FakeCollector, FakeSource, start_core

READ_TIMEOUT_S = 5.0


async def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + READ_TIMEOUT_S
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _command(request_id: str = "w-1", *, plan_rev: int = 3) -> WriteCommand:
    return WriteCommand(request_id, tag_id=1, value=42.5, plan_rev=plan_rev, timeout_ms=6000)


async def _connected(core: Core, channel: aio.Channel) -> FakeCollector:
    core.source.definitions = sample_definitions(rev=3)
    await _until(lambda: core.server.summary().plan_rev == 3)
    collector = FakeCollector(channel)
    collector.hello()
    assert (await collector.read()).WhichOneof("msg") == "plan"
    return collector


async def _next_write(collector: FakeCollector) -> pb.WriteRequest:
    while True:
        message = await collector.read()
        if message.WhichOneof("msg") == "write":
            return message.write


async def test_send_delivers_request_and_returns_matching_result() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        collector = await _connected(core, channel)
        service = core.service
        sent = asyncio.create_task(service.send(_command(), wait_s=5.0))
        request = await _next_write(collector)
        assert (request.request_id, request.tag_id, request.value) == ("w-1", 1, 42.5)
        assert (request.plan_rev, request.timeout_ms) == (3, 6000)
        # 다른 id의 결과(늦은 응답)는 무시하고, 같은 id 결과를 돌려준다.
        collector.send(
            pb.CollectorMsg(write_result=pb.WriteResult(request_id="old", status=pb.WriteResult.OK))
        )
        collector.send(
            pb.CollectorMsg(
                write_result=pb.WriteResult(
                    request_id="w-1",
                    status=pb.WriteResult.OK,
                    readback=42.5,
                    readback_ts_unix_ns=123,
                )
            )
        )
        assert await sent == WriteReply(WriteReplyStatus.OK, "", 42.5, 123)
        await collector.close()


async def test_status_mapping_and_missing_readback() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        collector = await _connected(core, channel)
        cases = [
            (pb.WriteResult.REJECTED, WriteReplyStatus.REJECTED),
            (pb.WriteResult.FAILED, WriteReplyStatus.FAILED),
            (pb.WriteResult.INDETERMINATE, WriteReplyStatus.INDETERMINATE),
            (pb.WriteResult.OK, WriteReplyStatus.OK),
            # 이후 버전이 더한 status는 결과 모름
            (pb.WriteResult.Status.ValueType(7), WriteReplyStatus.INDETERMINATE),
        ]
        for index, (raw, expected) in enumerate(cases):
            request_id = f"w-{index}"
            sent = asyncio.create_task(core.service.send(_command(request_id), wait_s=5.0))
            assert (await _next_write(collector)).request_id == request_id
            result = pb.WriteResult(request_id=request_id, status=raw, error="boom")
            collector.send(pb.CollectorMsg(write_result=result))
            reply = await sent
            assert reply.status is expected, raw
            assert (reply.readback, reply.readback_ts_ns) == (None, None)
            assert ("unknown write status 7" in reply.error) is (raw == 7)
        await collector.close()


async def test_no_result_within_wait_is_no_response_and_late_result_is_ignored() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        collector = await _connected(core, channel)
        reply = await core.service.send(_command(), wait_s=0.2)
        assert reply.status is WriteReplyStatus.NO_RESPONSE
        assert "no write result within 0.2s" in reply.error
        await _next_write(collector)
        collector.send(
            pb.CollectorMsg(write_result=pb.WriteResult(request_id="w-1", status=pb.WriteResult.OK))
        )
        collector.batch(0)  # 늦은 결과 뒤에도 세션은 정상(Ack)
        assert (await collector.read()).ack.seq == 0
        await collector.close()


async def test_session_end_resolves_pending_write_as_no_response() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        collector = await _connected(core, channel)
        sent = asyncio.create_task(core.service.send(_command(), wait_s=5.0))
        await _next_write(collector)
        collector.cancel()
        reply = await asyncio.wait_for(sent, READ_TIMEOUT_S)
        assert reply == WriteReply(WriteReplyStatus.NO_RESPONSE, "collector session closed")


async def test_send_needs_exactly_one_session_and_unique_request_id() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        none = await core.service.send(_command(), wait_s=1.0)
        assert none.status is WriteReplyStatus.REJECTED
        assert "0 collector session(s)" in none.error

        first = await _connected(core, channel)
        pending = asyncio.create_task(core.service.send(_command("dup"), wait_s=5.0))
        await _next_write(first)
        duplicate = await core.service.send(_command("dup"), wait_s=1.0)
        assert duplicate.status is WriteReplyStatus.REJECTED
        assert "in use" in duplicate.error

        second = FakeCollector(channel)
        second.hello()
        assert (await second.read()).WhichOneof("msg") == "plan"
        two = await core.service.send(_command("w-2"), wait_s=1.0)
        assert (two.status, "2 collector session(s)" in two.error) == (
            WriteReplyStatus.REJECTED,
            True,
        )
        await second.close()
        first.send(
            pb.CollectorMsg(write_result=pb.WriteResult(request_id="dup", status=pb.WriteResult.OK))
        )
        assert (await pending).status is WriteReplyStatus.OK
        await first.close()


async def test_write_path_reports_live_batch_plan_rev_and_device() -> None:
    async with start_core() as core, aio.insecure_channel(core.target) as channel:
        assert core.service.write_path("plc-1").sessions == 0
        collector = await _connected(core, channel)
        path = core.service.write_path("plc-1")
        assert (path.sessions, path.plan_rev, path.reported_plan_rev) == (1, 3, None)
        assert (path.device_connected, path.device_timeout_ms) == (False, 1000)
        assert core.service.write_path("nope").device_timeout_ms is None

        collector.batch(0, plan_rev=2)  # 이전 plan으로 보낸 heartbeat
        assert (await collector.read()).ack.seq == 0
        assert core.service.write_path("plc-1").reported_plan_rev == 2
        collector.batch(0, plan_rev=3)
        assert (await collector.read()).ack.seq == 0
        assert core.service.write_path("plc-1").reported_plan_rev == 3
        collector.batch(9, [], replay=True, plan_rev=1)  # replay는 원래 rev — 세지 않는다
        assert (await collector.read()).ack.seq == 9
        assert core.service.write_path("plc-1").reported_plan_rev == 3

        health = pb.DeviceHealth(device_name="plc-1", connected=True, status="ok")
        collector.send(pb.CollectorMsg(health=health))
        await _until(lambda: core.service.write_path("plc-1").device_connected)
        await collector.close()


async def test_plan_keeper_marks_only_writable_tags() -> None:
    applied: list[pb.AcquisitionPlan] = []

    async def apply(plan: pb.AcquisitionPlan, _keys: Sequence[TagKey]) -> None:
        applied.append(plan)

    def writable(definitions: Definitions) -> frozenset[int]:
        assert definitions.rev == 3
        return frozenset({2})

    source = FakeSource(sample_definitions(rev=3))
    assert await PlanKeeper(source, apply, refresh_s=1.0, writable=writable).refresh()
    assert await PlanKeeper(source, apply, refresh_s=1.0).refresh()
    flagged, default = applied
    assert [(t.tag_id, t.writable) for t in flagged.tags] == [(1, False), (2, True)]
    assert [t.writable for t in default.tags] == [False, False]
