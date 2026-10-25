"""legacy 항목 하나(Modbus TCP 연결·Modbus 태그) → 프로젝트 문서 매핑(M5-1, PD-12).

각 매핑 함수는 변환된 문서를 돌려주거나, 변환할 수 없으면 `Notes.unsupported`에 이유를 남기고
None을 돌려준다. 변환된 항목에서 V1 스키마로 옮길 수 없는 필드는 `kind="field"` 항목으로 남긴다
(조용히 버리지 않는다). 비밀값으로 보이는 필드는 값을 옮기지 않고 `secret://<device id>` 참조로만
바꾼다. 값은 legacy pydantic lax 규칙(`legacy_values`)으로 읽는다.

주소 기준(legacy `drivers/modbus_client.py`): 연결 파일 안 `tags:`는 0-based로 그대로 쓰였고,
TagStore 태그(`tags/*.json|yaml`)의 Modbus 주소는 연결의 `zero_based_addressing`이 false(기본)이면
1-based라 1을 뺐다. V1 collector는 0-based를 그대로 쓰므로 같은 규칙으로 옮긴다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from pureiron_project.canonical import Json
from pureiron_project.legacy_values import (
    DESCRIPTION_MAX,
    Fields,
    Notes,
    as_bool,
    as_int,
    as_number,
    as_text,
    new_id,
    put_text,
    slug,
)

TAG_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$")
EU_MAX = 32
HOST_MAX = 253
MIN_MS = 50
MAX_MS = 2**31 - 1
PORT_MAX = 65535
UNIT_ID_MAX = 255
ADDRESS_MAX = 65535
# legacy `ModbusConnectionConfig.timeout_s` 기본값. V1 기본 1000 ms와 달라 키가 없어도 적는다.
LEGACY_TIMEOUT_S = 3.0
_PROVIDER = re.compile(r"^\[[^\]]*\]")
# legacy project_package.py `_REDACT_KEY_PATTERN`과 같은 패턴·camelCase 정규화(apiKey → api_Key).
SECRET_KEY = re.compile(
    r"(^|[_-])(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"certificate|credential|hashed[_-]?password|username|email|phone|mobile|"
    r"avatar[_-]?url|contact|location)([_-]|$)",
    re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
REGISTER_TYPES = {
    "holding": "holding",
    "input": "input",
    "coil": "coil",
    "discrete_input": "discrete",
}
BIT_REGISTERS = frozenset({"coil", "discrete"})
# legacy `ModbusDataType`(float64 없음 — legacy가 거부하던 값은 변환하지 않는다).
DATA_TYPES = frozenset({"bool", "int16", "uint16", "int32", "uint32", "float32"})
ORDERS = frozenset({"big", "little"})
_ADDRESSING_FLAGS = ("zero_based_addressing", "reverse_word_order")
# 연결 파일에서 장비 변환에 쓰는 키(나머지는 field로 보고).
_CONNECTION_KEYS = ("name", "protocol", "host", "port", "unit_id", "timeout_s", "scan_rate_ms")


@dataclass(frozen=True, slots=True)
class Device:
    """변환된 장비 + 그 연결에 속한 legacy 태그 주소 규칙."""

    legacy_name: str
    doc: dict[str, Json]
    zero_based: bool
    reverse_word_order: bool


def tag_path(legacy: str) -> str | None:
    """`[Provider]Folder/Tag` → `Folder/Tag`. V1 tagPath로 쓸 수 없으면 None."""
    path = _PROVIDER.sub("", legacy.strip(), count=1)
    return path if TAG_PATH.match(path) else None


def is_secret_key(key: str) -> bool:
    return SECRET_KEY.search(_CAMEL.sub("_", key)) is not None


def _secret_keys(data: Mapping[str, Json]) -> list[str]:
    return sorted(k for k, v in data.items() if is_secret_key(k) and v not in (None, ""))


def _addressing(
    raw: Mapping[str, Json], file: str, item: str, notes: Notes
) -> tuple[bool, bool] | None:
    flags = [as_bool(raw.get(key, False)) for key in _ADDRESSING_FLAGS]
    for key, flag in zip(_ADDRESSING_FLAGS, flags, strict=True):
        if flag is None:
            notes.add(file, item, "modbus_connection", f"{key} is not a boolean")
    zero_based, reverse = flags
    return None if zero_based is None or reverse is None else (zero_based, reverse)


def connection_device(
    raw: Mapping[str, Json], file: str, item: str, taken: set[str], notes: Notes
) -> Device | None:
    """Modbus TCP 연결(legacy `ModbusConnectionConfig`) → `devices/<id>.yaml`."""
    name = as_text(raw.get("name")) or ""
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
    port = as_int(raw.get("port", 502), 1, PORT_MAX)
    unit_id = as_int(raw.get("unit_id", 1), 0, UNIT_ID_MAX)
    host_ok = isinstance(host, str) and 1 <= len(host) <= HOST_MAX
    if not host_ok or port is None or unit_id is None:
        notes.add(file, item, "modbus_connection", "host/port/unit_id out of the V1 range")
        return None
    addressing = _addressing(raw, file, item, notes)
    if addressing is None:
        return None
    doc: dict[str, Json] = {
        "id": device_id,
        "protocol": "modbus_tcp",
        "host": host,
        "port": port,
        "unit_id": unit_id,
    }
    fields = Fields(raw)
    for key in (*_CONNECTION_KEYS, *_ADDRESSING_FLAGS, "tags"):
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
    zero_based, reverse = addressing
    return Device(legacy_name=name, doc=doc, zero_based=zero_based, reverse_word_order=reverse)


def _timing(doc: dict[str, Json], raw: Mapping[str, Json], note: tuple[Notes, str, str]) -> None:
    notes, file, item = note
    if "scan_rate_ms" in raw:
        poll_ms = as_int(raw["scan_rate_ms"], MIN_MS, MAX_MS)
        if poll_ms is None:
            notes.dropped(
                file, item, "scan_rate_ms", f"poll interval must be an integer {MIN_MS}..{MAX_MS}"
            )
        else:
            doc["poll_ms"] = poll_ms
    seconds = as_number(raw.get("timeout_s", LEGACY_TIMEOUT_S))
    millis = seconds * 1000 if seconds is not None else math.nan
    if not MIN_MS <= millis <= MAX_MS:
        notes.dropped(file, item, "timeout_s", f"timeout must be {MIN_MS}..{MAX_MS} ms")
    else:
        doc["timeout_ms"] = round(millis)


@dataclass(frozen=True, slots=True)
class TagSource:
    """legacy 태그 한 개의 위치·주소 규칙. inline = 연결 파일 안 `tags:`(0-based 그대로)."""

    file: str
    device: Device
    inline: bool


def _bad_path(legacy_path: str, file: str, notes: Notes) -> None:
    """한글 등 비ASCII 경로는 따로 센다 — tagPath 유니코드 확장은 별도 결정 대기(M5-1 리뷰 Q2)."""
    stripped = _PROVIDER.sub("", legacy_path.strip(), count=1)
    if stripped and not stripped.isascii():
        notes.add(
            file,
            legacy_path,
            "non_ascii_tag_path",
            "non-ASCII tag path (V1 tagPath is ASCII; schema decision pending)",
        )
    else:
        notes.add(
            file,
            legacy_path,
            "modbus_tag",
            "path cannot be expressed as a V1 tag path (A-Z a-z 0-9 _ . / -)",
        )


def modbus_tag(
    legacy_path: str, address: Mapping[str, Json], source: TagSource, notes: Notes
) -> dict[str, Json] | None:
    """Modbus 주소(legacy `ModbusTagMapping`·`ModbusAddressConfig`) → `tags/<device>.yaml` 항목."""
    file = source.file
    path = tag_path(legacy_path)
    if path is None:
        _bad_path(legacy_path, file, notes)
        return None
    zero_based = source.inline or source.device.zero_based
    # 1-based 주소는 1을 빼므로 65536까지 V1 주소 범위 안이다.
    highest = ADDRESS_MAX if zero_based else ADDRESS_MAX + 1
    register_type = REGISTER_TYPES.get(str(address.get("register_type", "holding")))
    raw_address = as_int(address.get("address", 0), 0, highest)
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
    legacy_path = as_text(raw.get("tag_path")) or ""
    tag = modbus_tag(legacy_path, raw, source, notes)
    if tag is None:
        return None
    fields = Fields(raw)
    for key in ("tag_path", "register_type", "address", "data_type", "byte_order", "word_order"):
        fields.get(key)
    note = (notes, source.file, legacy_path, "description")
    put_text(tag, "description", fields.get("description"), DESCRIPTION_MAX, note)
    note = (notes, source.file, legacy_path, "engineering_units")
    put_text(tag, "eu", fields.get("engineering_units"), EU_MAX, note)
    for key in fields.rest():
        notes.dropped(source.file, legacy_path, key, "tag mapping field has no V1 equivalent")
    return tag


# legacy YAML 태그(`config_loader`)가 최상위에서 읽던 tag_config 키 — TagDefinition 필드가 아니다.
YAML_CONFIG_KEYS = frozenset(
    {
        "tag_type",
        "device_name",
        "address_config",
        "scan_rate_ms",
        "read_only",
        "protocol",
        "opc_server",
        "opc_item_path",
        "connection",
        "node_id",
        "expression_string",
        "query",
        "datasource",
        "query_type",
        "initial_value",
        "is_retained",
    }
)


def _infer_tag_type(raw: Mapping[str, Json]) -> str:
    """legacy `config_loader._infer_tag_type` 그대로(괄호 없는 경로는 provider가 있어도 힌트 없음
    — legacy 연산자 우선순위)."""
    explicit = raw.get("tag_type")
    if explicit:
        return str(explicit)
    path = str(raw.get("path", ""))
    hint = ""
    if "[" in path:
        hint = str(raw.get("provider", "")).lower() or path[1 : path.find("]")].lower()
    if "opc" in hint or "modbus" in hint:
        return "Device"
    if raw.get("expression_string"):
        return "Expression"
    if raw.get("query") and raw.get("datasource"):
        return "Query"
    return "Memory"


def yaml_tag_config(raw: Mapping[str, Json]) -> dict[str, Json]:
    """legacy YAML 태그의 tag_config(`_build_tag_config`) — 최상위 키에서 만든다.

    `address_config`가 없으면 평면 형식: `protocol`(기본 OPC-UA)이 OPC-UA가 아니면 Modbus holding,
    주소 = `opc_item_path`, 장비 = `opc_server` 또는 `connection`.
    """
    tag_type = _infer_tag_type(raw)
    config: dict[str, Json] = {"tag_type": tag_type}
    if tag_type not in {"Device", "OPC"}:
        return config
    config["scan_rate_ms"] = raw.get("scan_rate_ms", LEGACY_TAG_SCAN_MS)
    # legacy YAML 로더는 `bool(raw_tag.get("read_only", False))` — 파이썬 참거짓이다.
    config["read_only"] = bool(raw.get("read_only", False))
    if "address_config" in raw:
        config["device_name"] = raw.get("device_name", "")
        config["address_config"] = raw["address_config"]
    elif raw.get("protocol", "OPC-UA") == "OPC-UA":
        config["address_config"] = {"protocol": "opcua"}
    else:
        config["device_name"] = raw.get("opc_server") or raw.get("connection", "")
        config["address_config"] = {"protocol": "modbus", "address": raw.get("opc_item_path") or 0}
    return config


# TagStore 태그(`TagDefinition`)에서 따로 읽는 필드와, 값이 이것과 같으면 옮길 것이 없는 필드.
# provider·id·data_type은 legacy 내부 표기(경로 접두·DB 키·값 형식)라 옮길 대상이 아니다.
# source_kind는 수집 정책 판정(convert_legacy `_history_reason`)이 읽는다.
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
        "source_kind",
    }
)
LEGACY_TAG_SCAN_MS = 1000
_DEFINITION_DEFAULTS: dict[str, Json] = {"scan_group": "default"}
# legacy `ModbusAddressConfig`·`DeviceTagConfig` 필드. 그 밖의 키는 legacy pydantic도 무시했다.
_ADDRESS_KEYS = frozenset(
    {"protocol", "register_type", "address", "data_type", "byte_order", "word_order"}
)
_DEVICE_CONFIG_KEYS = frozenset(
    {"tag_type", "device_name", "address_config", "scan_rate_ms", "read_only"}
)


def is_yaml(file: str) -> bool:
    return file.endswith((".yaml", ".yml"))


def _ignored_config_keys(config: Mapping[str, Json], file: str) -> list[tuple[str, str]]:
    """`tag_config`(JSON만 — YAML은 최상위 키로 만든다)·`address_config` 안의 모르는 키."""
    ignored: list[tuple[str, str]] = []
    if not is_yaml(file):
        ignored = [
            (f"tag_config.{key}", "legacy DeviceTagConfig ignored this field")
            for key in sorted(config)
            if key not in _DEVICE_CONFIG_KEYS
        ]
    address = config.get("address_config")
    ignored.extend(
        (f"address_config.{key}", "legacy ModbusAddressConfig ignored this field")
        for key in sorted(address if isinstance(address, dict) else {})
        if key not in _ADDRESS_KEYS
    )
    return ignored


def _store_tag_options(
    raw: Mapping[str, Json], config: Mapping[str, Json], file: str
) -> list[tuple[str, str]]:
    """옮기지 못하는 TagStore 태그 설정 → (필드, 이유) 목록."""
    dropped: list[tuple[str, str]] = []
    known = _DEFINITION_KNOWN
    if is_yaml(file):
        known = _DEFINITION_KNOWN | YAML_CONFIG_KEYS
        if "tag_config" in raw:
            dropped.append(("tag_config", "legacy YAML loader ignored tag_config (top-level keys)"))
    dropped.extend(_ignored_config_keys(config, file))
    # legacy 기본값은 쓰기 가능(read_only False) — 키가 없어도 V1에서는 읽기 전용이 된다.
    if as_bool(config.get("read_only", False)) is not True:
        reason = "legacy write permission is not carried over; declare write: explicitly"
        dropped.append(("read_only", reason))
    if config.get("scan_rate_ms", LEGACY_TAG_SCAN_MS) != LEGACY_TAG_SCAN_MS:
        reason = "per-tag scan rate; V1 polls every tag at its device poll_ms"
        dropped.append(("scan_rate_ms", reason))
    metadata = raw.get("metadata")
    for key, value in sorted(metadata.items() if isinstance(metadata, dict) else []):
        if key == "history_enabled" and value is True:
            continue
        reason = (
            "V1 records every tag (record.* only tunes deadband/interval)"
            if key == "history_enabled"
            else "tag metadata has no V1 equivalent"
        )
        dropped.append((f"metadata.{key}", reason))
    for key in sorted(raw):
        value = raw[key]
        if key in known or value in (None, "", [], {}):
            continue
        if _DEFINITION_DEFAULTS.get(key) != value:
            dropped.append((key, "tag field has no V1 equivalent"))
    return dropped


def store_tag(
    raw: Mapping[str, Json],
    config: Mapping[str, Json],
    address: Mapping[str, Json],
    source: TagSource,
    notes: Notes,
) -> dict[str, Json] | None:
    """TagStore Device 태그(`address_config.protocol == "modbus"`) 한 항목. config = tag_config."""
    legacy_path = as_text(raw.get("path")) or ""
    tag = modbus_tag(legacy_path, address, source, notes)
    if tag is None:
        return None
    note = (notes, source.file, legacy_path, "description")
    put_text(tag, "description", raw.get("description"), DESCRIPTION_MAX, note)
    note = (notes, source.file, legacy_path, "engineering_units")
    put_text(tag, "eu", raw.get("engineering_units"), EU_MAX, note)
    for key, reason in _store_tag_options(raw, config, source.file):
        notes.dropped(source.file, legacy_path, key, reason)
    return tag
