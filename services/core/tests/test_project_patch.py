"""현장 PATCH 정책(M5-2 D1'·P1·P2): 허용·거부 필드, `null` 제거, 기본값 생략 정규형, no-op 판정,
감사용 바뀐 필드."""

from __future__ import annotations

import pytest

from pureiron_core.project import patch
from pureiron_core.project.patch import PatchKind, changed_fields, merge, normalize
from pureiron_core.project.ports import PatchInvalidError
from pureiron_project import absorb

ALARM = {"id": "tank-high", "tag": "plant/level", "type": "above", "setpoint": 80, "deadband": 2}
TAG = {
    "path": "plant/level",
    "device": "plc-1",
    "modbus": {"register_type": "holding", "address": 0, "data_type": "float32"},
    "eu": "m",
    "record": {"deadband": 0.5, "max_interval_s": 60},
}


def test_absorb_takes_exactly_the_site_patchable_fields() -> None:
    """벤더 absorb(M5-3 K2)는 현장이 PATCH로 바꿀 수 있는 필드만 repo로 옮긴다."""
    assert absorb.ALARM_FIELDS == patch.ALARM_FIELDS
    assert absorb.TAG_FIELDS == patch.TAG_FIELDS
    assert set(patch.RECORD_FIELDS) == {"deadband", "max_interval_s"}  # record 통째로 옮겨도 된다


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},  # D1': M5-2에서 뺀 필드(스키마에 없음)
        {"type": "below"},
        {"tag": "plant/other"},
        {"id": "other"},
        {"description": "설명은 알람에서 허용하지 않는다"},
        {"setpoint": 81, "unknown": 1},
    ],
)
def test_alarm_rejects_fields_outside_d1(changes: dict[str, object]) -> None:
    with pytest.raises(PatchInvalidError, match="not patchable"):
        merge(PatchKind.ALARM, ALARM, changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"modbus": {"register_type": "holding", "address": 1, "data_type": "float32"}},
        {"device": "plc-2"},
        {"write": {"min": 0, "max": 10}},
        {"path": "plant/renamed"},
    ],
)
def test_tag_rejects_fields_outside_d1(changes: dict[str, object]) -> None:
    with pytest.raises(PatchInvalidError, match="not patchable"):
        merge(PatchKind.TAG, TAG, changes)


def test_tag_rejects_unknown_record_field_and_empty_record() -> None:
    with pytest.raises(PatchInvalidError, match="record fields not patchable"):
        merge(PatchKind.TAG, TAG, {"record": {"mode": "change"}})
    with pytest.raises(PatchInvalidError, match="non-empty object"):
        merge(PatchKind.TAG, TAG, {"record": {}})


def test_empty_body_and_null_setpoint_are_invalid() -> None:
    with pytest.raises(PatchInvalidError, match="empty"):
        merge(PatchKind.ALARM, ALARM, {})
    with pytest.raises(PatchInvalidError, match="required"):
        merge(PatchKind.ALARM, ALARM, {"setpoint": None})


def test_alarm_operating_values_merge_into_existing_definition() -> None:
    merged = merge(
        PatchKind.ALARM,
        ALARM,
        {"setpoint": 85.5, "on_delay_s": 3, "priority": "critical"},
    )
    assert merged == {
        "id": "tank-high",
        "tag": "plant/level",
        "type": "above",
        "setpoint": 85.5,
        "deadband": 2,
        "on_delay_s": 3,
        "priority": "critical",
    }


def test_null_removes_optional_field() -> None:
    assert "deadband" not in merge(PatchKind.ALARM, ALARM, {"deadband": None})
    tag = merge(PatchKind.TAG, TAG, {"eu": None, "record": {"deadband": None}})
    assert tag == {
        "path": "plant/level",
        "device": "plc-1",
        "modbus": {"register_type": "holding", "address": 0, "data_type": "float32"},
        "record": {"max_interval_s": 60},
    }


def test_removing_last_record_field_or_null_record_drops_record() -> None:
    once = merge(PatchKind.TAG, TAG, {"record": {"deadband": None, "max_interval_s": None}})
    assert "record" not in once
    assert "record" not in merge(PatchKind.TAG, TAG, {"record": None})


def test_record_patch_keeps_sibling_value() -> None:
    tag = merge(PatchKind.TAG, TAG, {"record": {"deadband": 1.25}, "description": "탱크 수위"})
    assert tag["record"] == {"deadband": 1.25, "max_interval_s": 60}
    assert tag["description"] == "탱크 수위"


def test_values_equal_to_schema_default_are_omitted() -> None:
    """정규형은 기본값 생략 — export 트리 바이트가 값 표현에 흔들리지 않게(R3)."""
    merged = merge(PatchKind.ALARM, ALARM, {"deadband": 0, "on_delay_s": 0.0, "priority": "medium"})
    assert merged == {"id": "tank-high", "tag": "plant/level", "type": "above", "setpoint": 80}


def test_numbers_and_strings_are_canonical() -> None:
    """정수로 표현되는 float는 정수(JCS), 문자열은 NFC — 파일 apply가 저장하는 값과 같다."""
    merged = merge(PatchKind.ALARM, ALARM, {"setpoint": 90.0})
    assert merged["setpoint"] == 90
    assert isinstance(merged["setpoint"], int)
    tag = merge(PatchKind.TAG, TAG, {"description": "é"})  # e + 결합 악센트(NFD)
    assert tag["description"] == "é"


@pytest.mark.parametrize(
    ("kind", "changes", "fragment"),
    [
        (PatchKind.ALARM, {"deadband": -1}, "minimum"),
        (PatchKind.ALARM, {"priority": "urgent"}, "is not one of"),
        (PatchKind.ALARM, {"setpoint": "80"}, "not of type"),
        (PatchKind.TAG, {"eu": "x" * 33}, "too long"),
        (PatchKind.TAG, {"record": {"max_interval_s": 0}}, "minimum"),
        (PatchKind.TAG, {"record": {"max_interval_s": 1.5}}, "not of type"),
    ],
)
def test_merged_definition_must_pass_schema(
    kind: PatchKind, changes: dict[str, object], fragment: str
) -> None:
    current = ALARM if kind is PatchKind.ALARM else TAG
    with pytest.raises(PatchInvalidError, match=fragment):
        merge(kind, current, changes)


def test_same_value_is_noop_after_normalization() -> None:
    """파일에 기본값을 적어 둔 정의도 정규형으로 비교하면 같은 값 PATCH는 변화가 없다."""
    stored = {**ALARM, "priority": "medium", "on_delay_s": 0}
    before = normalize(PatchKind.ALARM, stored)
    assert merge(PatchKind.ALARM, before, {"setpoint": 80, "priority": "medium"}) == before


def test_changed_fields_lists_only_differences_with_record_flattened() -> None:
    before = normalize(PatchKind.TAG, TAG)
    after = merge(PatchKind.TAG, before, {"eu": "cm", "record": {"deadband": None}})
    assert changed_fields(before, after) == (
        {"eu": "m", "record.deadband": 0.5},
        {"eu": "cm", "record.deadband": None},
    )
