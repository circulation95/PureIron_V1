from __future__ import annotations

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.plan import DEFAULT_TIMEOUT_MS, parse_plan


def tag(
    tag_id: int,
    device_id: str,
    *,
    register_type: str = "holding",
    data_type: str = "uint16",
    byte_order: str = "",
    word_order: str = "",
) -> pb.TagPoint:
    address = pb.ModbusAddress(
        register_type=register_type,
        address=tag_id,
        data_type=data_type,
        byte_order=byte_order,
        word_order=word_order,
    )
    return pb.TagPoint(tag_id=tag_id, device_id=device_id, modbus=address)


def test_parse_groups_tags_by_device_and_fills_defaults() -> None:
    plan = pb.AcquisitionPlan(
        plan_rev=3,
        default_poll_ms=500,
        devices=[
            pb.Device(device_id="a", protocol="modbus_tcp", host="h", port=502, unit_id=1),
            pb.Device(
                device_id="b",
                protocol="modbus_tcp",
                host="h",
                port=503,
                poll_ms=200,
                timeout_ms=300,
            ),
        ],
        tags=[tag(1, "a"), tag(2, "b"), tag(3, "a", data_type="float32", word_order="little")],
    )
    parsed = parse_plan(plan)
    assert parsed.problems == ()
    a, b = parsed.devices
    assert (a.poll_ms, a.timeout_ms, [p.tag_id for p in a.points]) == (
        500,
        DEFAULT_TIMEOUT_MS,
        [1, 3],
    )
    assert (b.poll_ms, b.timeout_ms) == (200, 300)
    assert (a.points[0].byte_order, a.points[1].word_order) == ("big", "little")


def test_parse_skips_invalid_entries_and_reports_them() -> None:
    plan = pb.AcquisitionPlan(
        plan_rev=1,
        devices=[
            pb.Device(device_id="ok", protocol="modbus_tcp", host="h", port=502),
            pb.Device(device_id="s7", protocol="s7", host="h", port=102),
        ],
        tags=[
            tag(1, "ok"),
            tag(2, "ok", register_type="weird"),
            tag(3, "ok", data_type="int64"),
            tag(4, "ok", byte_order="middle"),
            tag(5, "ghost"),
            pb.TagPoint(tag_id=6, device_id="ok"),
        ],
    )
    parsed = parse_plan(plan)
    assert [d.device_id for d in parsed.devices] == ["ok"]
    assert [p.tag_id for p in parsed.devices[0].points] == [1]
    assert parsed.devices[0].poll_ms == 1000  # default_poll_ms도 0이면 1s
    joined = "\n".join(parsed.problems)
    for needle in ("tag 2", "tag 3", "tag 4", "tag 6", "unsupported protocol 's7'", "device ghost"):
        assert needle in joined
