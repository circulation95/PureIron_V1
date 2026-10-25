"""legacy 항목 하나(Modbus TCP 연결·Modbus 태그·상하한 알람) → 프로젝트 문서 매핑(M5-1, PD-12).

각 매핑 함수는 변환된 문서를 돌려주거나, 변환할 수 없으면 `Notes.unsupported`에 이유를 남기고
None을 돌려준다. 변환된 항목에서 V1 스키마로 옮길 수 없는 필드는 `kind="field"` 항목으로 남긴다
(조용히 버리지 않는다). 비밀값으로 보이는 필드는 값을 옮기지 않고 `secret://<device id>` 참조로만
바꾼다.

주소 기준(legacy `drivers/modbus_client.py`): 연결 파일 안 `tags:`는 0-based로 그대로 쓰였고,
TagStore 태그(`tags/*.json`)의 Modbus 주소는 연결의 `zero_based_addressing`이 false(기본)이면
1-based라 1을 뺐다. V1 collector는 0-based를 그대로 쓰므로 같은 규칙으로 옮긴다.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from pureiron_project.canonical import Json

ID_MAX = 63
TAG_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$")
DESCRIPTION_MAX = 500
EU_MAX = 32
MIN_MS = 50
PORT_MAX = 65535
UNIT_ID_MAX = 255
ADDRESS_MAX = 65535
_PROVIDER = re.compile(r"^\[[^\]]*\]")
_NOT_ID = re.compile(r"[^a-z0-9]+")
# legacy project_package.py의 비밀값·개인정보 키 패턴과 같은 기준.
SECRET_KEY = re.compile(
    r"(^|[_-])(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"certificate|credential|hashed[_-]?password|username|email|phone|mobile)([_-]|$)",
    re.IGNORECASE,
)
REGISTER_TYPES = {
    "holding": "holding",
    "input": "input",
    "coil": "coil",
    "discrete_input": "discrete",
}
BIT_REGISTERS = frozenset({"coil", "discrete"})
DATA_TYPES = frozenset({"bool", "int16", "uint16", "int32", "uint32", "float32", "float64"})
ORDERS = frozenset({"big", "little"})
ALARM_TYPES = {"abovesetpoint": "above", "belowsetpoint": "below"}
PRIORITIES = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}
# 연결 파일에서 장비 변환에 쓰는 키(나머지는 field로 보고).
_CONNECTION_KEYS = (
    "name",
    "protocol",
    "host",
    "port",
    "unit_id",
    "timeout_s",
    "scan_rate_ms",
    "tags",
    "zero_based_addressing",
    "reverse_word_order",
)


@dataclass(frozen=True, slots=True)
class Entry:
    """보고서 `unsupported[]` 한 줄. field는 변환된 항목에서 옮기지 못한 필드 이름."""

    file: str
    item: str
    kind: str
    reason: str
    field: str | None = None

    def as_json(self) -> dict[str, Json]:
        out: dict[str, Json] = {
            "file": self.file,
            "item": self.item,
            "kind": self.kind,
            "reason": self.reason,
        }
        if self.field is not None:
            out["field"] = self.field
        return out


@dataclass(slots=True)
class Notes:
    unsupported: list[Entry] = field(default_factory=list[Entry])
    secrets: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])

    def add(self, file: str, item: str, kind: str, reason: str) -> None:
        self.unsupported.append(Entry(file, item, kind, reason))

    def dropped(self, file: str, item: str, key: str, reason: str, *, kind: str = "field") -> None:
        self.unsupported.append(Entry(file, item, kind, reason, key))


@dataclass(frozen=True, slots=True)
class Device:
    """변환된 장비 + 그 연결에 속한 legacy 태그 주소 규칙."""

    legacy_name: str
    doc: dict[str, Json]
    zero_based: bool
    reverse_word_order: bool


class Fields:
    """읽은 키를 기억해, 읽지 않은 키를 field 항목으로 보고할 수 있게 한다."""

    def __init__(self, data: Mapping[str, Json]) -> None:
        self._data = data
        self._used: set[str] = set()

    def get(self, key: str) -> Json:
        self._used.add(key)
        return self._data.get(key)

    def rest(self) -> list[str]:
        return sorted(k for k in self._data if k not in self._used)


def slug(text: str) -> str:
    """legacy 이름 → V1 id(`^[a-z0-9][a-z0-9-]{0,62}$`). 쓸 글자가 없으면 빈 문자열."""
    lowered = unicodedata.normalize("NFKC", text).lower()
    return _NOT_ID.sub("-", lowered).strip("-")[:ID_MAX].rstrip("-")


def new_id(name: str, prefix: str, taken: set[str]) -> str | None:
    """이름 → 쓰지 않은 id. 쓸 글자가 없는 이름(예: 한글만)은 `<prefix>-<n>`(입력 순서대로),
    slug가 이미 쓰인 이름은 legacy에서도 충돌이므로 None."""
    base = slug(name)
    if base:
        return None if base in taken else base
    n = 1
    while f"{prefix}-{n}" in taken:
        n += 1
    return f"{prefix}-{n}"


def tag_path(legacy: str) -> str | None:
    """`[Provider]Folder/Tag` → `Folder/Tag`. V1 tagPath로 쓸 수 없으면 None."""
    path = _PROVIDER.sub("", legacy.strip(), count=1)
    return path if TAG_PATH.match(path) else None


def _int(value: Json, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _number(value: Json) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _text(value: Json) -> str | None:
    return value if isinstance(value, str) else None


def _optional_text(
    doc: dict[str, Json], key: str, value: Json, limit: int, note: tuple[Notes, str, str, str]
) -> None:
    """빈 문자열은 생략, 길이 초과·문자열 아님은 field로 보고."""
    if value is None or value == "":
        return
    if isinstance(value, str) and len(value) <= limit:
        doc[key] = value
        return
    notes, file, item, legacy_key = note
    notes.dropped(file, item, legacy_key, f"not a string of at most {limit} characters")


def _secret_keys(data: Mapping[str, Json]) -> list[str]:
    return sorted(k for k, v in data.items() if SECRET_KEY.search(k) and v not in (None, ""))


def connection_device(
    raw: Mapping[str, Json], file: str, item: str, taken: set[str], notes: Notes
) -> Device | None:
    """Modbus TCP 연결(legacy `ModbusConnectionConfig`) → `devices/<id>.yaml`."""
    name = _text(raw.get("name")) or ""
    protocol = raw.get("protocol", "tcp")
    if protocol != "tcp":
        notes.add(
            file,
            item,
            "modbus_rtu_connection" if protocol == "rtu" else "connection",
            f"protocol {protocol!r} is not Modbus TCP",
        )
        return None
    device_id = new_id(name, "device", taken)
    if device_id is None:
        notes.add(file, item, "modbus_connection", f"duplicate device id {slug(name)!r}")
        return None
    host = raw.get("host", "localhost")
    port = _int(raw.get("port", 502), 1, PORT_MAX)
    unit_id = _int(raw.get("unit_id", 1), 0, UNIT_ID_MAX)
    if not isinstance(host, str) or not host or port is None or unit_id is None:
        notes.add(file, item, "modbus_connection", "host/port/unit_id out of the V1 range")
        return None
    doc: dict[str, Json] = {
        "id": device_id,
        "protocol": "modbus_tcp",
        "host": host,
        "port": port,
        "unit_id": unit_id,
    }
    fields = Fields(raw)
    for key in _CONNECTION_KEYS:
        fields.get(key)
    _timing(doc, raw, (notes, file, item))
    secret_keys = _secret_keys(raw)
    if secret_keys:
        doc["credentials"] = f"secret://{device_id}"
        notes.secrets.extend(
            {"file": file, "item": item, "field": k, "ref": f"secret://{device_id}"}
            for k in secret_keys
        )
    for key in fields.rest():
        if key not in secret_keys:
            notes.dropped(file, item, key, "connection setting has no V1 device field")
    taken.add(device_id)
    return Device(
        legacy_name=name,
        doc=doc,
        zero_based=raw.get("zero_based_addressing") is True,
        reverse_word_order=raw.get("reverse_word_order") is True,
    )


def _timing(doc: dict[str, Json], raw: Mapping[str, Json], note: tuple[Notes, str, str]) -> None:
    notes, file, item = note
    if "scan_rate_ms" in raw:
        poll_ms = _int(raw["scan_rate_ms"], MIN_MS, 2**31 - 1)
        if poll_ms is None:
            notes.dropped(
                file, item, "scan_rate_ms", f"poll interval must be an integer >= {MIN_MS}"
            )
        else:
            doc["poll_ms"] = poll_ms
    if "timeout_s" in raw:
        seconds = _number(raw["timeout_s"])
        timeout_ms = round(seconds * 1000) if seconds is not None else None
        if timeout_ms is None or timeout_ms < MIN_MS:
            notes.dropped(file, item, "timeout_s", f"timeout must be at least {MIN_MS} ms")
        else:
            doc["timeout_ms"] = timeout_ms


@dataclass(frozen=True, slots=True)
class TagSource:
    """legacy 태그 한 개의 위치·주소 규칙. inline = 연결 파일 안 `tags:`(0-based 그대로)."""

    file: str
    device: Device
    inline: bool


def modbus_tag(
    legacy_path: str, address: Mapping[str, Json], source: TagSource, notes: Notes
) -> dict[str, Json] | None:
    """Modbus 주소(legacy `ModbusTagMapping`·`ModbusAddressConfig`) → `tags/<device>.yaml` 항목."""
    file = source.file
    path = tag_path(legacy_path)
    if path is None:
        notes.add(
            file,
            legacy_path,
            "modbus_tag",
            "path cannot be expressed as a V1 tag path (A-Z a-z 0-9 _ . / -)",
        )
        return None
    register_type = REGISTER_TYPES.get(str(address.get("register_type", "holding")))
    raw_address = _int(address.get("address", 0), 0, ADDRESS_MAX)
    data_type = address.get("data_type", "uint16")
    byte_order = address.get("byte_order", "big")
    word_order = address.get("word_order", "big")
    if (
        register_type is None
        or raw_address is None
        or data_type not in DATA_TYPES
        or byte_order not in ORDERS
        or word_order not in ORDERS
    ):
        notes.add(
            file, legacy_path, "modbus_tag", "register_type/address/data_type/order unsupported"
        )
        return None
    zero_based = source.inline or source.device.zero_based
    modbus: dict[str, Json] = {
        "register_type": register_type,
        "address": raw_address if zero_based else max(0, raw_address - 1),
        "data_type": data_type,
        "byte_order": byte_order,
        "word_order": "little"
        if source.device.reverse_word_order and not source.inline
        else word_order,
    }
    if register_type in BIT_REGISTERS and data_type != "bool":
        modbus["data_type"] = "bool"
        notes.dropped(
            file,
            legacy_path,
            "data_type",
            f"{register_type} is a bit; {data_type!r} converted as bool",
        )
    return {"path": path, "device": source.device.doc["id"], "modbus": modbus}


def inline_tag(raw: Mapping[str, Json], source: TagSource, notes: Notes) -> dict[str, Json] | None:
    """연결 파일 안 `tags:` 한 항목."""
    legacy_path = _text(raw.get("tag_path")) or ""
    tag = modbus_tag(legacy_path, raw, source, notes)
    if tag is None:
        return None
    fields = Fields(raw)
    for key in ("tag_path", "register_type", "address", "data_type", "byte_order", "word_order"):
        fields.get(key)
    note = (notes, source.file, legacy_path, "description")
    _optional_text(tag, "description", fields.get("description"), DESCRIPTION_MAX, note)
    note = (notes, source.file, legacy_path, "engineering_units")
    _optional_text(tag, "eu", fields.get("engineering_units"), EU_MAX, note)
    for key in fields.rest():
        notes.dropped(source.file, legacy_path, key, "tag mapping field has no V1 equivalent")
    return tag


# TagStore 태그(`TagDefinition`)에서 따로 읽는 필드와, 값이 이것과 같으면 옮길 것이 없는 필드.
# provider·id·data_type은 legacy 내부 표기(경로 접두·DB 키·값 형식)라 옮길 대상이 아니다.
# tag_type 등 5개는 YAML 평면 형식에서 tag_config 대신 최상위에 오는 키(`TAG_CONFIG_KEYS`).
TAG_CONFIG_KEYS = ("tag_type", "device_name", "address_config", "scan_rate_ms", "read_only")
_DEFINITION_KNOWN = frozenset(
    {
        "path",
        "provider",
        "id",
        "data_type",
        "description",
        "engineering_units",
        "metadata",
        "tag_config",
        *TAG_CONFIG_KEYS,
    }
)
LEGACY_TAG_SCAN_MS = 1000
_DEFINITION_DEFAULTS: dict[str, Json] = {"scan_group": "default", "source_kind": "authored"}


def store_tag(
    raw: Mapping[str, Json],
    config: Mapping[str, Json],
    address: Mapping[str, Json],
    source: TagSource,
    notes: Notes,
) -> dict[str, Json] | None:
    """TagStore Device 태그(`address_config.protocol == "modbus"`) 한 항목. config = tag_config."""
    legacy_path = _text(raw.get("path")) or ""
    tag = modbus_tag(legacy_path, address, source, notes)
    if tag is None:
        return None
    note = (notes, source.file, legacy_path, "description")
    _optional_text(tag, "description", raw.get("description"), DESCRIPTION_MAX, note)
    note = (notes, source.file, legacy_path, "engineering_units")
    _optional_text(tag, "eu", raw.get("engineering_units"), EU_MAX, note)
    if config.get("read_only") is False:
        notes.dropped(
            source.file,
            legacy_path,
            "read_only",
            "legacy write permission is not carried over; declare write: explicitly",
        )
    if config.get("scan_rate_ms", LEGACY_TAG_SCAN_MS) != LEGACY_TAG_SCAN_MS:
        notes.dropped(
            source.file,
            legacy_path,
            "scan_rate_ms",
            "per-tag scan rate; V1 polls every tag at its device poll_ms",
        )
    metadata = raw.get("metadata")
    for key, value in sorted(metadata.items() if isinstance(metadata, dict) else []):
        if key == "history_enabled" and value is True:
            continue
        reason = (
            "V1 records every tag (record.* only tunes deadband/interval)"
            if key == "history_enabled"
            else "tag metadata has no V1 equivalent"
        )
        notes.dropped(source.file, legacy_path, f"metadata.{key}", reason)
    for key in sorted(raw):
        value = raw[key]
        if key in _DEFINITION_KNOWN or value in (None, "", [], {}):
            continue
        if _DEFINITION_DEFAULTS.get(key) != value:
            notes.dropped(source.file, legacy_path, key, "tag field has no V1 equivalent")
    return tag


_ALARM_DEFAULTS: dict[str, Json] = {
    "setpoint_high": 0,
    "off_delay_s": 0,
    "shelve_duration_s": 3600,
    "notification_channels": [],
}


def alarm(
    raw: Mapping[str, Json], file: str, tags: Mapping[str, str], taken: set[str], notes: Notes
) -> dict[str, Json] | None:
    """상·하한 알람(legacy `AlarmConfig` AboveSetpoint/BelowSetpoint) → `alarms/*.yaml` 한 항목.

    tags: 변환된 태그의 legacy 경로 → V1 경로. 대상 태그가 변환되지 않았으면 알람도 옮기지 않는다
    (V1 validate가 모르는 태그를 가리키는 알람을 거부).
    """
    name = _text(raw.get("name")) or ""
    alarm_type = ALARM_TYPES.get(str(raw.get("alarm_type", "")).lower())
    legacy_tag = _text(raw.get("tag_path")) or ""
    setpoint = _number(raw.get("setpoint"))
    if alarm_type is None:
        notes.add(file, name, "alarm", f"alarm_type {raw.get('alarm_type')!r} is not above/below")
        return None
    if raw.get("enabled", True) is not True:
        notes.add(file, name, "alarm", "disabled alarm; V1 project alarms have no enabled flag")
        return None
    if legacy_tag not in tags:
        notes.add(file, name, "alarm", f"target tag {legacy_tag!r} was not converted")
        return None
    alarm_id = new_id(name, "alarm", taken)
    if alarm_id is None or setpoint is None:
        notes.add(file, name, "alarm", "duplicate alarm id or setpoint is not a number")
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
        value = _number(raw.get(key, 0))
        if value is None or value < 0:
            notes.dropped(file, name, key, "must be a number >= 0")
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
    _optional_text(
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
    known = {
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
    for key in sorted(k for k in raw if k not in known):
        notes.dropped(file, name, key, "alarm field has no V1 equivalent")
