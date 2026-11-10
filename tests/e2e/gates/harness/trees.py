"""프로젝트 트리 기대값 — 번들에 싣는 트리 디렉터리를 읽어(로컬 파일) 판정 기대값을 만든다."""

from __future__ import annotations

import ipaddress
import shutil
from pathlib import Path
from typing import cast

from harness import golden, paths
from harness.context import E2EError
from harness.judges import TreeExpect, judge_tree
from harness.release_pair import ReleasePair
from pureiron_project import canonical
from pureiron_project.project import load_project
from pureiron_project.schema import Kind

FIXTURE = paths.REPO / "packages" / "project-tools" / "fixtures" / "acme"


def field_endpoints(directory: Path) -> tuple[golden.Endpoint, ...]:
    """`directory` 트리가 선언한 modbus TCP 장비 끝점(host, port), 정렬·중복 제거. VM 번들의 트리
    (fixture `acme`·그 m5 사본·absorb 결과)는 장비가 같다. 스키마가 `port`를 필수로 두어 기본값은
    없다. nft `ip daddr`에 넣으므로 host는 IPv4 주소여야 한다."""
    project = load_project(directory)
    if not project.ok:
        raise E2EError(f"{directory}: " + "; ".join(project.problems))
    found: set[golden.Endpoint] = set()
    for f in project.files:
        device = f.data
        if f.kind is not Kind.DEVICE or not isinstance(device, dict):
            continue
        host, port = device.get("host"), device.get("port")
        if device.get("protocol") != "modbus_tcp" or not isinstance(host, str):
            continue
        if not isinstance(port, int):
            raise E2EError(f"{f.relpath}: port missing")
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise E2EError(f"{f.relpath}: host {host!r} is not an IPv4 address") from exc
        found.add((str(address), port))
    return tuple(sorted(found))


def tree_expect(directory: Path, pair: ReleasePair) -> TreeExpect:
    project = load_project(directory)
    if not project.ok:
        raise E2EError(f"{directory}: " + "; ".join(project.problems))
    keys = {Kind.DEVICE: "id", Kind.TAG_LIST: "path", Kind.ALARM_LIST: "id"}
    found: dict[Kind, set[str]] = {kind: set() for kind in keys}
    description: str | None = None
    for f in project.files:
        key = keys.get(f.kind)
        items = [f.data] if isinstance(f.data, dict) else f.data if isinstance(f.data, list) else []
        for item in items:
            value = item.get(key) if key is not None and isinstance(item, dict) else None
            if isinstance(value, str):
                found[f.kind].add(value)
            if f.kind is Kind.TAG_LIST and value == pair.tree_tag and isinstance(item, dict):
                text = item.get("description")
                description = text if isinstance(text, str) else None
    return TreeExpect(
        files=len(project.files),
        content_hash=project.content_hash(),
        devices=frozenset(found[Kind.DEVICE]),
        tags=frozenset(found[Kind.TAG_LIST]),
        alarms=frozenset(found[Kind.ALARM_LIST]),
        tree_tag=pair.tree_tag,
        tree_tag_description=description,
    )


def judge_fixture_tree(snap: dict[str, object], pair: ReleasePair) -> dict[str, bool]:
    """fixture 트리(`build_site_bundle` 기본값)를 실은 번들이 적용된 상태인가 — `judge_tree`."""
    return judge_tree(snap, tree_expect(FIXTURE, pair))


# ---------------------------------------------------------------- G2 100태그 트리(M6-2 E6)

SIM_DEVICE = "plc-1"
"""fixture `acme`의 modbus 장비(`10.0.0.5:502`) — G2에서는 VM 안 시뮬레이터가 이 주소에서 답한다."""
SIM_TAGS = 100


def g2_tag_paths() -> frozenset[str]:
    """G2 트리가 더하는 시뮬레이터 태그 경로(`sim/r000`~`sim/r099`)."""
    return frozenset(f"sim/r{address:03d}" for address in range(SIM_TAGS))


def g2_tags() -> list[canonical.Json]:
    """시뮬레이터 레지스터 0~99를 하나씩 읽는 태그 — 짝수 주소는 input, 홀수는 holding register
    (`uint16`, 시뮬레이터는 두 종류 모두 0~99에 사인파 값을 낸다). 주기는 장비 `poll_ms`(fixture는
    기본 1000 ms = 1 s)."""
    return [
        {
            "device": SIM_DEVICE,
            "modbus": {
                "address": address,
                "data_type": "uint16",
                "register_type": "input" if address % 2 == 0 else "holding",
            },
            "path": f"sim/r{address:03d}",
        }
        for address in range(SIM_TAGS)
    ]


def g2_tree(dest: Path, source: Path = FIXTURE) -> Path:
    """`source`(fixture `acme`) 사본의 `tags/plc-1.yaml`에 시뮬레이터 태그 100개를 더한 트리를
    `dest`에 새로 만든다(제품 fixture는 그대로). 결과가 유효한 프로젝트가 아니면 멈춘다."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    tag_file = dest / "tags" / f"{SIM_DEVICE}.yaml"
    items = cast(list[canonical.Json], canonical.load(tag_file.read_text("utf-8"), "tags"))
    tag_file.write_text(canonical.dump([*items, *g2_tags()]), encoding="utf-8", newline="\n")
    project = load_project(dest)
    if not project.ok:
        raise E2EError(f"{dest}: " + "; ".join(project.problems))
    return dest
