"""legacy 프로젝트 디렉터리 → 고객 프로젝트 트리 + 미지원 보고서(M5-1, PD-12, M5 plan D11).

지원: `project.json` → `project.yaml`, Modbus TCP 연결(`connections/*`, 장비 행 목록)
→ `devices/<id>.yaml`, Modbus 태그(연결 안 `tags:`, `tags/*.json|yaml`의 Device 태그)
→ `tags/<device>.yaml`, AboveSetpoint·BelowSetpoint 알람 → `alarms/<같은 이름>.yaml`.
그 밖의 모든 파일·항목(OPC-UA, UDT, scripts, dashboard, Memory·expression·query 태그, shelve,
`auth/`, `settings/`, 알 수 없는 파일)은 보고서 `unsupported[]`에 `{file, item, kind, reason}`로
빠짐없이 남는다.

계약:
- 결정적: 파일은 경로 정렬 순서로, 항목은 파일 안 순서로 읽고, 보고서에 시각·절대 경로를
  넣지 않는다 → 같은 입력이면 출력 트리·보고서가 바이트 단위로 같다.
- 비밀값·개인정보: `auth/`·`settings/`·사용자별 dashboard는 개수만 보고하고, 연결의 비밀값
  필드는 값을 옮기지 않고 `secret://<device id>` 참조로만 쓴다.
- 변환하는 파일(project.json·연결·태그·알람)을 읽을 수 없으면 `ConvertError`(아무것도 쓰지
  않음). 미지원 디렉터리의 파일은 읽지 못해도 파일 단위로 보고만 한다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pureiron_project.canonical import Json, dump
from pureiron_project.legacy_files import (
    DOCUMENT_SUFFIXES,
    ConvertError,
    item_name,
    mappings,
    read_document,
    report_only,
)
from pureiron_project.legacy_items import (
    Device,
    Notes,
    TagSource,
    alarm,
    connection_device,
    inline_tag,
    store_tag,
)
from pureiron_project.project import PROJECT_FILE
from pureiron_project.schema import Kind, omit_defaults

REPORT_FORMAT = "pureiron-convert-legacy-report"
REPORT_VERSION = 1
NAME_MAX = 200
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EXCLUDED_FILE = "tags/excluded_paths.json"
_APPROVED_FILE = "tags/approved_paths.json"
# 빈 디렉터리를 git에 남기는 표식 — 내용이 아니라 보고하지 않는다.
KEEP_FILE = ".gitkeep"


@dataclass(slots=True)
class Conversion:
    """변환 결과(메모리). `documents()`가 쓸 파일, `report()`가 보고서.

    sources = 변환 단계가 읽은 파일. 입력의 모든 파일은 sources 또는 `unsupported[].file`에 나온다.
    """

    project: dict[str, Json]
    files_total: int
    notes: Notes
    sources: list[str] = field(default_factory=list[str])
    devices: list[Device] = field(default_factory=list[Device])
    tags: dict[str, list[dict[str, Json]]] = field(default_factory=dict[str, list[dict[str, Json]]])
    alarms: dict[str, list[dict[str, Json]]] = field(
        default_factory=dict[str, list[dict[str, Json]]]
    )
    converted: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])

    def documents(self) -> list[tuple[str, Kind, Json]]:
        docs: list[tuple[str, Kind, Json]] = [(PROJECT_FILE, Kind.PROJECT, self.project)]
        for device in self.devices:
            device_id = str(device.doc["id"])
            docs.append((f"devices/{device_id}.yaml", Kind.DEVICE, device.doc))
            if self.tags.get(device_id):
                docs.append((f"tags/{device_id}.yaml", Kind.TAG_LIST, list(self.tags[device_id])))
        docs.extend(
            (rel, Kind.ALARM_LIST, list(items))
            for rel, items in sorted(self.alarms.items())
            if items
        )
        return docs

    def report(self, content_hash: str) -> dict[str, Json]:
        entries = sorted(self.notes.unsupported, key=lambda e: e.file.encode("utf-8"))
        by_kind: dict[str, Json] = {}
        for entry in entries:
            count = by_kind.get(entry.kind, 0)
            by_kind[entry.kind] = (count if isinstance(count, int) else 0) + 1
        return {
            "format": REPORT_FORMAT,
            "version": REPORT_VERSION,
            "project_id": self.project["project_id"],
            "content_hash": content_hash,
            "summary": {
                "files": self.files_total,
                "devices": len(self.devices),
                "tags": sum(len(v) for v in self.tags.values()),
                "alarms": sum(len(v) for v in self.alarms.values()),
                "unsupported": len(entries),
                "unsupported_by_kind": by_kind,
            },
            "converted": list(self.converted),
            "sources": list(self.sources),
            "secrets": list(self.notes.secrets),
            "unsupported": [e.as_json() for e in entries],
        }


def _project(raw: Json, notes: Notes) -> dict[str, Json]:
    if not isinstance(raw, dict):
        raise ConvertError("project.json: must be a JSON object")
    project_id, name = raw.get("project_id"), raw.get("name")
    if not isinstance(project_id, str) or not _ID.match(project_id):
        raise ConvertError(f"project.json: project_id {project_id!r} is not a valid V1 id")
    if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX:
        raise ConvertError(f"project.json: name must be a string of 1..{NAME_MAX} characters")
    if raw.get("schema_version", 1) != 1:
        raise ConvertError("project.json: only legacy schema_version 1 is known")
    features = raw.get("features")
    for key in sorted(features if isinstance(features, dict) else {}):
        notes.add(
            "project.json",
            f"features.{key}",
            "feature",
            "built-in legacy feature flag has no V1 equivalent",
        )
    for key in sorted(
        k for k in raw if k not in {"schema_version", "project_id", "name", "features"}
    ):
        notes.dropped("project.json", "$", key, "project field has no V1 equivalent")
    return {"schema_version": 1, "project_id": project_id, "name": name}


@dataclass(slots=True)
class _State:
    root: Path
    notes: Notes
    excluded: set[str] = field(default_factory=set[str])
    approved: set[str] | None = None
    device_ids: set[str] = field(default_factory=set[str])
    by_legacy_name: dict[str, Device] = field(default_factory=dict[str, Device])
    tag_paths: dict[str, str] = field(default_factory=dict[str, str])
    legacy_tags: dict[str, str] = field(default_factory=dict[str, str])
    alarm_ids: set[str] = field(default_factory=set[str])


def _policy(state: _State, rel: str) -> None:
    """legacy 수집 정책: excluded_paths = 지운 태그, approved_paths(있으면) = 수집 허용 목록."""
    data = read_document(state.root / rel, rel)
    paths = {p for p in data if isinstance(p, str)} if isinstance(data, list) else set[str]()
    if rel == _EXCLUDED_FILE:
        state.excluded = paths
    else:
        state.approved = paths


def _skip_reason(state: _State, legacy_path: str) -> str | None:
    if legacy_path in state.excluded:
        return f"excluded by {_EXCLUDED_FILE}"
    if state.approved is not None and legacy_path not in state.approved:
        return f"not in {_APPROVED_FILE} (legacy did not collect it)"
    return None


def _add_tag(
    conv: Conversion,
    state: _State,
    file: str,
    legacy_path: str,
    build: Callable[[], dict[str, Json] | None],
) -> None:
    """수집 정책·중복 검사 뒤 태그를 더한다. build는 그 뒤에만 불러 field 보고가 중복되지 않게."""
    skip = _skip_reason(state, legacy_path)
    if skip is not None:
        state.notes.add(file, legacy_path, "excluded_tag", skip)
        return
    if legacy_path in state.legacy_tags:
        state.notes.add(file, legacy_path, "modbus_tag", "duplicate legacy tag (first one kept)")
        return
    tag = build()
    if tag is None:
        return
    path = str(tag["path"])
    if path in state.tag_paths:
        state.notes.add(
            file,
            legacy_path,
            "modbus_tag",
            f"duplicate V1 tag path {path!r} (also {state.tag_paths[path]})",
        )
        return
    state.tag_paths[path] = file
    state.legacy_tags[legacy_path] = path
    conv.tags.setdefault(str(tag["device"]), []).append(tag)
    conv.converted.append({"kind": "tag", "file": file, "item": legacy_path, "to": path})


def _connection(
    conv: Conversion, state: _State, file: str, item: str, raw: Mapping[str, Json]
) -> None:
    device = connection_device(raw, file, item, state.device_ids, state.notes)
    inline = list(mappings(raw.get("tags")))
    if device is None:
        for index, tag in inline:
            legacy_path = item_name(tag, "tag_path", fallback=f"{item}.tags[{index}]")
            state.notes.add(
                file, legacy_path, "modbus_tag", f"connection {item!r} was not converted"
            )
        return
    conv.devices.append(device)
    state.by_legacy_name.setdefault(device.legacy_name, device)
    conv.converted.append({"kind": "device", "file": file, "item": item, "to": device.doc["id"]})
    source = TagSource(file, device, inline=True)
    for index, tag in inline:
        legacy_path = item_name(tag, "tag_path", fallback=f"{item}.tags[{index}]")
        _add_tag(
            conv, state, file, legacy_path, lambda tag=tag: inline_tag(tag, source, state.notes)
        )


def _opcua(state: _State, file: str, item: str, raw: Mapping[str, Json]) -> None:
    state.notes.add(
        file,
        item,
        "opcua_connection",
        "OPC-UA is not supported by the V1 collector (Modbus TCP only)",
    )
    for index, tag in mappings(raw.get("tags")):
        legacy_path = item_name(tag, "tag_path", fallback=f"{item}.tags[{index}]")
        state.notes.add(file, legacy_path, "opcua_tag", "OPC-UA tag (connection not converted)")


_ROW_KEYS = frozenset({"id", "name", "type", "hostname", "port", "config"})


def _device_row(
    conv: Conversion, state: _State, file: str, index: int, row: Mapping[str, Json]
) -> None:
    """legacy `devices` 테이블 행 모양({name, type, hostname, port, config}) 한 개."""
    item = item_name(row, "name", fallback=f"[{index}]")
    device_type = row.get("type")
    config = row.get("config")
    settings = config if isinstance(config, dict) else {}
    if device_type == "OPC UA":
        _opcua(state, file, item, settings)
        return
    if device_type != "Modbus TCP":
        state.notes.add(file, item, "device", f"device type {device_type!r} is not Modbus TCP")
        return
    for key in sorted(k for k in row if k not in _ROW_KEYS):
        state.notes.dropped(file, item, key, "device row field has no V1 equivalent")
    merged: dict[str, Json] = {
        "name": item,
        "protocol": "tcp",
        "host": row.get("hostname"),
        "port": row.get("port"),
    }
    merged.update(settings)
    _connection(conv, state, file, item, merged)


def _connection_file(conv: Conversion, state: _State, rel: str) -> None:
    data = read_document(state.root / rel, rel)
    if isinstance(data, dict) and isinstance(data.get("connections"), list):
        for index, raw in mappings(data["connections"]):
            _connection(
                conv, state, rel, item_name(raw, "name", fallback=f"connections[{index}]"), raw
            )
    elif isinstance(data, dict) and "endpoint_url" in data:
        _opcua(state, rel, item_name(data, "name", fallback=rel), data)
    elif _is_device_rows(data):
        for index, row in mappings(data):
            _device_row(conv, state, rel, index, row)
    else:
        state.notes.add(rel, "$", "file", "unrecognised connection file")


def _is_device_rows(data: Json) -> bool:
    return isinstance(data, list) and any("hostname" in row for _, row in mappings(data))


def _tag_config(raw: Mapping[str, Json]) -> dict[str, Json] | None:
    """`tag_config` 블록. legacy YAML 평면 형식(`tag_type`·`address_config`가 최상위)도 받는다."""
    config = raw.get("tag_config")
    if isinstance(config, dict):
        return config
    if "address_config" in raw or "tag_type" in raw:
        return {k: raw[k] for k in ("tag_type", "device_name", "address_config") if k in raw}
    return None


_TAG_KINDS = {
    "Memory": ("memory_tag", "memory tags are not part of V1 (device tags only)"),
    "Expression": ("expression_tag", "expression tags are not supported"),
    "Query": ("query_tag", "query tags are not supported"),
}


def _store_tag(
    conv: Conversion, state: _State, file: str, index: int, raw: Mapping[str, Json]
) -> None:
    legacy_path = item_name(raw, "path", fallback=f"[{index}]")
    config = _tag_config(raw)
    if config is None:
        status = raw.get("metadata")
        status = status.get("status") if isinstance(status, dict) else None
        state.notes.add(
            file,
            legacy_path,
            "tag",
            f"no tag_config (legacy import placeholder, status {status!r})",
        )
        return
    tag_type = config.get("tag_type", "Memory")
    if tag_type in _TAG_KINDS:
        kind, reason = _TAG_KINDS[str(tag_type)]
        state.notes.add(file, legacy_path, kind, reason)
        return
    address = config.get("address_config")
    protocol = address.get("protocol") if isinstance(address, dict) else None
    device = state.by_legacy_name.get(str(config.get("device_name", "")))
    if tag_type not in {"Device", "OPC"} or protocol not in {"modbus", "opcua"}:
        state.notes.add(
            file,
            legacy_path,
            "device_tag",
            f"tag type {tag_type!r} / protocol {protocol!r} is not supported",
        )
    elif protocol == "opcua":
        state.notes.add(file, legacy_path, "opcua_tag", "OPC-UA tag")
    elif device is None:
        state.notes.add(
            file,
            legacy_path,
            "modbus_tag",
            f"device {config.get('device_name')!r} is not a converted Modbus TCP connection",
        )
    else:
        source = TagSource(file, device, inline=False)
        _add_tag(conv, state, file, legacy_path, lambda: store_tag(raw, source, state.notes))


def _tag_file(conv: Conversion, state: _State, rel: str, data: Json) -> None:
    if isinstance(data, dict) and isinstance(data.get("tags"), list):
        data = data["tags"]
    if not isinstance(data, list):
        state.notes.add(rel, "$", "file", "unrecognised tag file")
        return
    for index, raw in mappings(data):
        _store_tag(conv, state, rel, index, raw)


def _alarm_file(conv: Conversion, state: _State, rel: str) -> None:
    data = read_document(state.root / rel, rel)
    items = data.get("alarms") if isinstance(data, dict) else None
    if not isinstance(items, list):
        state.notes.add(rel, "$", "file", "unrecognised alarm file")
        return
    out = conv.alarms.setdefault(rel, [])
    for index, raw in mappings(items):
        item = item_name(raw, "name", fallback=f"alarms[{index}]")
        doc = alarm(raw, rel, state.legacy_tags, state.alarm_ids, state.notes)
        if doc is not None:
            out.append(doc)
            conv.converted.append({"kind": "alarm", "file": rel, "item": item, "to": doc["id"]})


def _files(root: Path, notes: Notes) -> list[str]:
    rels: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            notes.add(rel, "$", "file", "symbolic link not followed")
        elif path.is_file() and path.name != KEEP_FILE:
            rels.append(rel)
    return sorted(rels, key=lambda r: r.encode("utf-8"))


_AREAS = {"connections": "connection", "tags": "tags", "alarms": "alarms"}


def _area(rel: str) -> str:
    """파일 경로 → 처리 단계. 변환 단계는 정해진 위치·확장자만 받는다(legacy 로더와 같은 범위)."""
    parts = rel.split("/")
    suffix = Path(rel).suffix
    if rel == "project.json":
        return "project"
    if rel in {_EXCLUDED_FILE, _APPROVED_FILE}:
        return "policy"
    if len(parts) != 2 or suffix not in DOCUMENT_SUFFIXES:
        return "other"
    if parts[0] == "alarms" and suffix != ".yaml":
        return "other"
    return _AREAS.get(parts[0], "other")


def convert_legacy(root: Path) -> Conversion:
    """legacy 디렉터리를 읽어 변환 결과를 만든다(디스크에 쓰지 않음).

    순서가 계약이다: 수집 정책 → 연결(장비) → 태그 파일의 장비 행 → 태그 → 알람 → 보고 전용 파일.
    태그는 장비가, 알람은 태그가 먼저 있어야 변환된다.
    """
    if not root.is_dir():
        raise ConvertError(f"{root}: not a directory")
    notes = Notes()
    files = _files(root, notes)
    if "project.json" not in files:
        raise ConvertError("project.json: missing")
    areas: dict[str, list[str]] = {}
    for rel in files:
        areas.setdefault(_area(rel), []).append(rel)
    project = _project(read_document(root / "project.json", "project.json"), notes)
    conv = Conversion(project=project, files_total=len(files), notes=notes)
    conv.sources = [rel for rel in files if _area(rel) != "other"]
    state = _State(root, notes)
    for rel in areas.get("policy", []):
        _policy(state, rel)
    for rel in areas.get("connection", []):
        _connection_file(conv, state, rel)
    tag_docs = [(rel, read_document(root / rel, rel)) for rel in areas.get("tags", [])]
    for rel, data in tag_docs:
        if _is_device_rows(data):
            for index, row in mappings(data):
                _device_row(conv, state, rel, index, row)
    for rel, data in tag_docs:
        if not _is_device_rows(data):
            _tag_file(conv, state, rel, data)
    for rel in areas.get("alarms", []):
        _alarm_file(conv, state, rel)
    for rel in areas.get("other", []):
        report_only(root, rel, notes)
    return conv


def write_tree(conv: Conversion, out: Path) -> None:
    """정규형(기본값 생략·`dump`)으로 쓴다 → `fmt --check`가 바꿀 것이 없다."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ConvertError(f"{out}: output directory must be empty or absent")
    for rel, kind, data in conv.documents():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(dump(omit_defaults(kind, data)).encode("utf-8"))


def report_bytes(report: Json) -> bytes:
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
