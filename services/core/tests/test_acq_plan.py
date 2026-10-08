"""DB 정의 → AcquisitionPlan: 기본값 채움·포트 기본값·비활성 장비 태그 제외."""

from __future__ import annotations

from pureiron_core.acquisition.plan import build_plan
from pureiron_core.project.ports import Definitions, DeviceDef, TagDef
from pureiron_core.tags.ports import TagKey

from .conftest import sample_definitions


def test_defaults_are_filled_from_canonical_definitions() -> None:
    built = build_plan(sample_definitions(rev=7))
    plan = built.plan
    assert plan.plan_rev == 7
    assert plan.default_poll_ms == 1000
    (device,) = plan.devices
    assert (device.device_id, device.protocol, device.host, device.port) == (
        "plc-1",
        "modbus_tcp",
        "10.0.0.5",
        502,
    )
    assert (device.unit_id, device.poll_ms, device.timeout_ms) == (1, 1000, 1000)
    temp, running = plan.tags
    assert temp.WhichOneof("address") == "modbus"
    assert (temp.tag_id, temp.device_id) == (1, "plc-1")
    assert (temp.modbus.register_type, temp.modbus.address, temp.modbus.data_type) == (
        "holding",
        0,
        "float32",
    )
    assert (temp.modbus.byte_order, temp.modbus.word_order) == ("big", "big")
    assert running.modbus.register_type == "coil"
    assert built.keys == (
        TagKey(1, "line1/temp", "plc-1"),
        TagKey(2, "line1/running", "plc-1"),
    )
    assert built.problems == ()


def test_explicit_values_win_and_unit_id_zero_is_kept() -> None:
    defs = Definitions(
        rev=1,
        devices=(
            DeviceDef(
                "d",
                {
                    "id": "d",
                    "protocol": "modbus_tcp",
                    "host": "h",
                    "port": 5020,
                    "unit_id": 0,
                    "poll_ms": 250,
                    "timeout_ms": 400,
                },
            ),
        ),
        tags=(
            TagDef(
                9,
                "t",
                "d",
                {
                    "modbus": {
                        "register_type": "input",
                        "address": 10,
                        "data_type": "int32",
                        "byte_order": "little",
                        "word_order": "little",
                    }
                },
            ),
        ),
    )
    plan = build_plan(defs).plan
    (device,) = plan.devices
    assert (device.port, device.unit_id, device.poll_ms, device.timeout_ms) == (5020, 0, 250, 400)
    modbus = plan.tags[0].modbus
    assert (modbus.address, modbus.byte_order, modbus.word_order) == (10, "little", "little")


def test_missing_port_gets_protocol_default() -> None:
    """collector는 port 0인 장비를 거부한다 — 비어 있으면 Modbus TCP 기본 502."""
    defs = Definitions(
        rev=1, devices=(DeviceDef("d", {"id": "d", "protocol": "modbus_tcp", "host": "h"}),)
    )
    assert build_plan(defs).plan.devices[0].port == 502


def test_tags_of_inactive_device_and_unsupported_address_are_reported() -> None:
    base = sample_definitions()
    defs = Definitions(
        rev=base.rev,
        devices=base.devices,
        tags=(
            *base.tags,
            TagDef(3, "gone/x", "retired-dev", {"modbus": {"register_type": "holding"}}),
            TagDef(4, "line1/odd", "plc-1", {"opcua": {}}),
        ),
    )
    built = build_plan(defs)
    assert [t.tag_id for t in built.plan.tags] == [1, 2]
    assert [k.tag_id for k in built.keys] == [1, 2]
    assert len(built.problems) == 2
    assert "retired-dev" in built.problems[0]
    assert "no supported address" in built.problems[1]


def test_empty_definitions_give_empty_plan() -> None:
    plan = build_plan(Definitions(rev=0)).plan
    assert (plan.plan_rev, list(plan.devices), list(plan.tags)) == (0, [], [])
