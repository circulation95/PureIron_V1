"""계약 JSON Schema 3종이 2020-12로 유효하고 plan §6.2/§6.3 예시 문서를 받아들이는지."""

from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Protocol, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[1]
SHA = "sha256:" + "a" * 64


def _schema(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((CONTRACTS / name).read_text(encoding="utf-8")))


class _Validator(Protocol):
    def validate(self, instance: object) -> None: ...

    def iter_errors(self, instance: object) -> Iterator[object]: ...


def _registry() -> Registry[Any]:
    resources = [
        (str(s["$id"]), Resource.from_contents(s))
        for s in (_schema(f"{n}.schema.json") for n in ("project", "manifest", "fleet-site"))
    ]
    return Registry[Any]().with_resources(resources)


def _validator(name: str, ref: str | None = None) -> _Validator:
    schema = _schema(f"{name}.schema.json")
    target: dict[str, object] = {"$ref": f"{schema['$id']}{ref}"} if ref else schema
    validator = Draft202012Validator(target, registry=_registry(), format_checker=FormatChecker())
    return cast(_Validator, validator)


@pytest.mark.parametrize("name", ["project", "manifest", "fleet-site"])
def test_schema_is_valid_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(_schema(f"{name}.schema.json"))


FLEET_SITE = {
    "site": "site-a",
    "generation": 17,
    "product": "2026.11.0",
    "project": {"repo": "pureiron-project-acme", "version": "v3"},
    "window": {
        "recurring": {"tz": "Asia/Seoul", "days": ["sat", "sun"], "start": "02:00", "end": "05:00"}
    },
}

MANIFEST = {
    "format": 1,
    "kind": "site",
    "site_id": "site-a",
    "generation": 17,
    "created_at": "2026-11-02T01:00:00Z",
    "product_release": {
        "version": "2026.11.0",
        "min_updater": "1.0.0",
        "images": {
            "core": {
                "config_digest": SHA,
                "registry_digest": SHA,
                "tag": "pureiron/core:2026.11.0",
            },
            "collector": {"config_digest": SHA, "tag": "pureiron/collector:2026.11.0"},
            "web": {"config_digest": SHA, "tag": "pureiron/web:2026.11.0"},
            "site-db": {
                "config_digest": SHA,
                "upstream": "timescale/timescaledb:2.22.1-pg16@" + SHA,
                "tag": "pureiron/site-db:2.22.1-pg16",
            },
        },
        "schema": {
            "target": 9,
            "app_expected": 9,
            "app_max_compatible": 10,
            "compat_app_min": "2026.10.0",
        },
    },
    "project": {
        "project_id": "acme",
        "version": "v3",
        "content_hash": SHA,
        "absorbs": {"site-a": 3},
    },
    "window": {"once": {"start": "2026-11-02T10:03:00+09:00", "end": "2026-11-02T11:03:00+09:00"}},
    "files": {"images/core.tar.gz": SHA, "project/project.tar": SHA},
    "trust_update": None,
    "fleet_commit": "abc123",
}

PROJECT = {
    "schema_version": 1,
    "project_id": "acme",
    "name": "ACME Line 1",
    "absorbs": {"site-a": 3},
    "historian": {"retention_days": 365, "default_deadband": 0.0, "default_max_interval_s": 60},
}


def test_examples_validate() -> None:
    _validator("fleet-site").validate(FLEET_SITE)
    _validator("manifest").validate(MANIFEST)
    _validator("project").validate(PROJECT)


def test_project_file_kinds_validate() -> None:
    _validator("project", "#/$defs/device").validate(
        {
            "id": "plc-1",
            "protocol": "modbus_tcp",
            "host": "10.0.0.5",
            "port": 502,
            "credentials": "secret://plc1",
        }
    )
    _validator("project", "#/$defs/tagList").validate(
        [
            {
                "path": "line1/temp",
                "device": "plc-1",
                "modbus": {"register_type": "holding", "address": 0, "data_type": "float32"},
                "record": {"deadband": 0.5, "max_interval_s": 60},
            }
        ]
    )
    _validator("project", "#/$defs/alarmList").validate(
        [{"id": "temp-high", "tag": "line1/temp", "type": "above", "setpoint": 80}]
    )


@pytest.mark.parametrize(
    ("name", "document", "ref"),
    [
        ("fleet-site", {**FLEET_SITE, "generation": 0}, None),
        (
            "fleet-site",
            {
                **FLEET_SITE,
                "window": {"recurring": {"tz": "x", "days": [], "start": "2:00", "end": "05:00"}},
            },
            None,
        ),
        ("manifest", {**MANIFEST, "format": 2}, None),
        ("manifest", {**MANIFEST, "files": {"../escape": SHA}}, None),
        ("project", {**PROJECT, "extra": True}, None),
        ("project", {"id": "plc-1", "protocol": "s7", "host": "h", "port": 102}, "#/$defs/device"),
        (
            "project",
            {
                "id": "plc-1",
                "protocol": "modbus_tcp",
                "host": "h",
                "port": 502,
                "credentials": "plain",
            },
            "#/$defs/device",
        ),
    ],
)
def test_invalid_documents_are_rejected(name: str, document: object, ref: str | None) -> None:
    assert list(_validator(name, ref).iter_errors(document))
