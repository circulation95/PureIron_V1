"""legacy 상·하한 알람(`AlarmConfig` Above/BelowSetpoint) → `alarms/*.yaml` 한 항목(M5-1, PD-12).

legacy 알람 키는 `"{tag_path}::{name}"`이라 태그마다 `High`·`Low` 같은 이름을 다시 쓰는 것이
정상이다.
V1 알람 id는 프로젝트 전체에서 하나라, slug(name)가 이미 쓰였으면 `slug(V1 태그 경로-name)`으로
결정적으로 바꾸고, 그것도 쓰였을 때만 보고한다.
"""

from __future__ import annotations

from collections.abc import Mapping

from pureiron_project.canonical import Json
from pureiron_project.legacy_values import (
    DESCRIPTION_MAX,
    Notes,
    as_bool,
    as_number,
    as_text,
    new_id,
    put_text,
    slug,
)

ALARM_TYPES = {"abovesetpoint": "above", "belowsetpoint": "below"}
PRIORITIES = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}
_ALARM_DEFAULTS: dict[str, Json] = {
    "setpoint_high": 0,
    "off_delay_s": 0,
    "shelve_duration_s": 3600,
    "notification_channels": [],
}
_KNOWN = frozenset(
    {
        "name",
        "enabled",
        "tag_path",
        "alarm_type",
        "setpoint",
        "deadband",
        "on_delay_s",
        "priority",
        "description",
        *_ALARM_DEFAULTS,
    }
)


def _alarm_id(name: str, tag: str, taken: set[str]) -> str | None:
    alarm_id = new_id(name, "alarm", taken)
    if alarm_id is not None:
        return alarm_id
    alternate = slug(f"{tag}-{name}")
    return alternate if alternate and alternate not in taken else None


def alarm(
    raw: Mapping[str, Json], file: str, tags: Mapping[str, str], taken: set[str], notes: Notes
) -> dict[str, Json] | None:
    """tags: 변환된 태그의 legacy 경로 → V1 경로. 대상 태그가 변환되지 않았으면 알람도 옮기지 않는다
    (V1 validate가 모르는 태그를 가리키는 알람을 거부)."""
    name = as_text(raw.get("name")) or ""
    alarm_type = ALARM_TYPES.get(str(raw.get("alarm_type", "")).lower())
    legacy_tag = as_text(raw.get("tag_path")) or ""
    setpoint = as_number(raw.get("setpoint"))
    enabled = as_bool(raw.get("enabled", True))
    if alarm_type is None:
        notes.add(file, name, "alarm", f"alarm_type {raw.get('alarm_type')!r} is not above/below")
        return None
    if enabled is not True:
        reason = "enabled is not a boolean" if enabled is None else "disabled alarm"
        notes.add(file, name, "alarm", f"{reason}; V1 project alarms have no enabled flag")
        return None
    if legacy_tag not in tags:
        notes.add(file, name, "alarm", f"target tag {legacy_tag!r} was not converted")
        return None
    if setpoint is None:
        notes.add(file, name, "alarm", "setpoint is not a finite number")
        return None
    alarm_id = _alarm_id(name, tags[legacy_tag], taken)
    if alarm_id is None:
        notes.add(file, name, "alarm", f"duplicate alarm id {slug(name)!r}")
        return None
    doc: dict[str, Json] = {
        "id": alarm_id,
        "tag": tags[legacy_tag],
        "type": alarm_type,
        "setpoint": setpoint,
    }
    _alarm_options(doc, raw, file, name, notes)
    taken.add(alarm_id)
    return doc


def _alarm_options(
    doc: dict[str, Json], raw: Mapping[str, Json], file: str, name: str, notes: Notes
) -> None:
    for key in ("deadband", "on_delay_s"):
        value = as_number(raw.get(key, 0))
        if value is None or value < 0:
            notes.dropped(file, name, key, "must be a finite number >= 0")
        else:
            doc[key] = value
    priority = str(raw.get("priority", "Medium")).lower()
    if priority == "diagnostic":
        doc["priority"] = "low"
        notes.dropped(file, name, "priority", "Diagnostic has no V1 priority; converted as low")
    elif priority in PRIORITIES:
        doc["priority"] = PRIORITIES[priority]
    else:
        notes.dropped(file, name, "priority", "unknown priority")
    put_text(
        doc,
        "description",
        raw.get("description"),
        DESCRIPTION_MAX,
        (notes, file, name, "description"),
    )
    for key, default in sorted(_ALARM_DEFAULTS.items()):
        if key in raw and raw[key] != default:
            if key == "shelve_duration_s":
                notes.dropped(file, name, key, "shelve is not part of V1 alarms", kind="shelve")
            else:
                notes.dropped(file, name, key, "alarm option has no V1 equivalent")
    for key in sorted(k for k in raw if k not in _KNOWN):
        notes.dropped(file, name, key, "alarm field has no V1 equivalent")
