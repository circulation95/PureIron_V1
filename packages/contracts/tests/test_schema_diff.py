from __future__ import annotations

import json
from pathlib import Path

import pytest
from schema_diff import compare, main

BASE: dict[str, object] = {
    "type": "object",
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z]+$"},
        "mode": {"enum": ["a", "b"]},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
        "value": {"type": ["number", "string"]},
    },
    "$defs": {"item": {"type": "object", "properties": {"x": {"type": "integer"}}}},
}


def _with(**changes: object) -> dict[str, object]:
    head = json.loads(json.dumps(BASE))
    for path, value in changes.items():
        node = head
        *parents, leaf = ("$defs" if key == "defs" else key for key in path.split("__"))
        for key in parents:
            node = node[key]
        if value is None:
            del node[leaf]
        else:
            node[leaf] = value
    return head


@pytest.mark.parametrize(
    ("head", "fragment"),
    [
        (_with(required=["id", "mode"]), "required added"),
        (_with(properties__mode=None), "properties.mode: removed"),
        (_with(properties__value={"type": "number"}), "type narrowed"),
        (_with(properties__mode={"enum": ["a"]}), "enum values removed"),
        (
            _with(properties__count={"type": "integer", "minimum": 1, "maximum": 10}),
            "minimum tightened",
        ),
        (
            _with(properties__count={"type": "integer", "minimum": 0, "maximum": 5}),
            "maximum tightened",
        ),
        (
            _with(properties__id={"type": "string", "pattern": "^[a-z0-9]+$"}),
            "pattern added or changed",
        ),
        (_with(additionalProperties=False), "additionalProperties closed"),
        (_with(defs__item=None), "$defs.item: removed"),
        (
            _with(properties__count={"type": "integer", "minimum": 0, "maximum": 10, "const": 3}),
            "const",
        ),
    ],
)
def test_breaking_changes(head: dict[str, object], fragment: str) -> None:
    found = compare(BASE, head)
    assert any(fragment in item for item in found), found


@pytest.mark.parametrize(
    "head",
    [
        BASE,
        _with(properties__note={"type": "string"}),
        _with(properties__mode={"enum": ["a", "b", "c"]}),
        _with(properties__count={"type": "integer", "minimum": 0}),
        _with(properties__value={"type": ["number", "string", "null"]}),
        _with(required=[]),
        _with(description="docs only"),
    ],
)
def test_compatible_changes(head: dict[str, object]) -> None:
    assert compare(BASE, head) == []


def test_defs_key_with_dollar_is_supported() -> None:
    head = json.loads(json.dumps(BASE))
    head["$defs"]["item"]["required"] = ["x"]
    assert any("$defs.item: required added" in item for item in compare(BASE, head))


def test_cli_exit_codes(tmp_path: Path) -> None:
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    base.write_text(json.dumps(BASE), encoding="utf-8")
    head.write_text(json.dumps(_with(properties__note={"type": "string"})), encoding="utf-8")
    assert main([str(base), str(head)]) == 0
    head.write_text(json.dumps(_with(required=["id", "count"])), encoding="utf-8")
    assert main([str(base), str(head)]) == 1
    assert main([str(base)]) == 2
