"""G1-6 property(M5-3a, 각 1,000 예): 프로젝트 트리 정규형과 드래프트 회수 왕복.

ⓐ 무작위 유효 트리를 JSON(정규형이 아닌 YAML)으로 쓴다 → `load_project` → `fmt`가 고정점에
   도달하고(두 번째 fmt는 바꿀 것이 없음) content hash는 fmt 전후로 같다.
ⓑ 무작위 base 트리에 무작위 PATCH 허용 값(설정·제거)을 적용한 드래프트를 서명해 base 사본에
   absorb → repo hash == 드래프트 `content_hash`.
ⓒ repo가 앞선 경우(base 뒤에 태그 추가): absorb가 그 태그를 보존한다 — repo hash == 드래프트
   트리 + 같은 추가 태그의 hash.

absorbs 줄 기준: absorb는 repo `project.yaml`에 `absorbs.<site> = site_rev`를 기록하므로, ⓑ·ⓒ의
hash 비교는 회수 뒤 repo `project.yaml` 데이터에서 `absorbs` 키를 지운 트리로 계산한다(base·
드래프트 트리는 absorbs가 없다). 기록 값 자체는 따로 `absorbs == {site: site_rev}`로 확인한다.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from pureiron_project.absorb import ABSORBS, absorb, absorbs_of
from pureiron_project.canonical import Json
from pureiron_project.project import PROJECT_FILE, Project, format_project, load_project

from .absorb_fakes import draft, enrollment, project_tar

SITE = "site-p"
SEED = bytes(range(7, 39))
SITE_REV = 9
BASE_SITE_REV = 4

type Tree = dict[str, Json]

_ids = st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True)
# JSON 실수 표기가 YAML 1.2 float로 그대로 읽히는 값(무한 소수·지수 표기 없이)
_numbers = st.one_of(
    st.integers(-100_000, 100_000), st.integers(-80_000, 80_000).map(lambda i: i / 8)
)
_non_negative = st.one_of(st.integers(0, 10_000), st.integers(0, 80_000).map(lambda i: i / 8))
_text = st.text(
    st.characters(categories=("L", "N", "P", "Zs")),
    min_size=1,
    max_size=24,
)
_priority = st.sampled_from(["low", "medium", "high", "critical"])


def _optional(**fields: st.SearchStrategy[Json]) -> st.SearchStrategy[dict[str, Json]]:
    return st.fixed_dictionaries({}, optional=fields)


_record = _optional(deadband=_non_negative, max_interval_s=st.integers(1, 3600)).filter(bool)


@st.composite
def _tag(draw: st.DrawFn, path: str, device: str) -> dict[str, Json]:
    modbus: dict[str, Json] = {
        "register_type": draw(st.sampled_from(["holding", "input"])),
        "address": draw(st.integers(0, 65_535)),
        "data_type": draw(st.sampled_from(["int16", "uint16", "float32"])),
    }
    if draw(st.booleans()):
        modbus["byte_order"] = "big"  # 명시한 기본값(정규형에서 생략)
    tag: dict[str, Json] = {"path": path, "device": device, "modbus": modbus}
    return tag | draw(_optional(eu=_text, description=_text, record=_record))


@st.composite
def _alarm(draw: st.DrawFn, alarm_id: str, tags: list[str]) -> dict[str, Json]:
    alarm: dict[str, Json] = {
        "id": alarm_id,
        "tag": draw(st.sampled_from(tags)),
        "type": draw(st.sampled_from(["above", "below"])),
        "setpoint": draw(_numbers),
    }
    extra = _optional(
        deadband=_non_negative, on_delay_s=_non_negative, priority=_priority, description=_text
    )
    return alarm | draw(extra)


@st.composite
def trees(draw: st.DrawFn) -> Tree:
    """유효한 프로젝트 트리(상대 경로 → 데이터). 기본값을 명시한 필드가 섞여 있다."""
    devices = draw(st.lists(_ids, min_size=1, max_size=3, unique=True))
    tree: Tree = {
        PROJECT_FILE: {"schema_version": 1, "project_id": "prop", "name": draw(_text)},
    }
    paths: list[str] = []
    for device in devices:
        tree[f"devices/{device}.yaml"] = {
            "id": device,
            "protocol": "modbus_tcp",
            "host": f"{device}.local",
            "port": draw(st.integers(1, 65_535)),
        } | draw(_optional(unit_id=st.just(1), poll_ms=st.integers(50, 5000)))
        count = draw(st.integers(1, 4))
        device_paths = [f"{device}/t{i}" for i in range(count)]
        paths.extend(device_paths)
        tree[f"tags/{device}.yaml"] = [draw(_tag(path, device)) for path in device_paths]
    alarm_count = draw(st.integers(0, 4))
    if alarm_count:
        tree["alarms/alarms.yaml"] = [draw(_alarm(f"a{i}", paths)) for i in range(alarm_count)]
    return tree


@st.composite
def _site_values(draw: st.DrawFn, item: Mapping[str, Json], alarm: bool) -> dict[str, Json]:
    """현장 PATCH 결과 값: PATCH 허용 필드를 새 값으로 바꾸거나 지운다(setpoint는 필수)."""
    changed = dict(item)
    fields = (
        {"deadband": _non_negative, "on_delay_s": _non_negative, "priority": _priority}
        if alarm
        else {"eu": _text, "description": _text, "record": _record}
    )
    if alarm and draw(st.booleans()):
        changed["setpoint"] = draw(_numbers)
    for field, values in fields.items():
        action = draw(st.sampled_from(["keep", "set", "remove"]))
        if action == "set":
            changed[field] = draw(values)
        elif action == "remove":
            changed.pop(field, None)
    return changed


@st.composite
def drafts(draw: st.DrawFn, base: Tree) -> Tree:
    """base 트리에 현장 PATCH를 적용한 유효 트리(항목마다 바꿀지 무작위)."""
    site: Tree = {}
    for rel, data in base.items():
        kind = rel.split("/", 1)[0]
        if kind not in ("tags", "alarms") or not isinstance(data, list):
            site[rel] = data
            continue
        items: list[Json] = []
        for item in data:
            assert isinstance(item, dict)
            patched = draw(st.booleans())
            items.append(draw(_site_values(item, kind == "alarms")) if patched else item)
        site[rel] = items
    return site


def _write(root: Path, tree: Tree) -> Path:
    """JSON(= YAML 1.2)으로 쓴다 — 정규형이 아닌 입력."""
    for rel, data in tree.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def _loaded(root: Path) -> Project:
    project = load_project(root)
    assert project.ok, project.problems
    return project


def _hash_without_absorbs(project: Project) -> str:
    files = [
        dataclasses.replace(f, data={k: v for k, v in f.data.items() if k != ABSORBS})
        if f.relpath == PROJECT_FILE and isinstance(f.data, dict)
        else f
        for f in project.files
    ]
    return Project(root=project.root, files=files).content_hash()


def _absorb_into(base: Tree, site: Tree, tmp: Path) -> tuple[Project, str]:
    """base를 repo로 쓰고 site 트리 드래프트를 회수 → (회수 뒤 repo, 드래프트 content_hash)."""
    tree, content_hash = project_tar(_write(tmp / "site", site))
    signed = draft(SEED, SITE, tree, content_hash, site_rev=SITE_REV, base_site_rev=BASE_SITE_REV)
    repo = _write(tmp / "repo", base)
    absorb(repo, signed, enrollment(SEED, SITE), SITE)
    after = _loaded(repo)
    assert absorbs_of(after) == {SITE: SITE_REV}
    return after, content_hash


@settings(max_examples=1000, deadline=None)
@given(trees())
def test_written_tree_reaches_fmt_fixed_point_with_stable_hash(tree: Tree) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _write(Path(tmp), tree)
        before = _loaded(root)
        format_project(before, check=False)
        after = _loaded(root)
        assert format_project(after, check=True) == []
        assert after.content_hash() == before.content_hash()


@settings(max_examples=1000, deadline=None)
@given(st.data())
def test_absorbed_repo_equals_draft_tree(data: st.DataObject) -> None:
    base = data.draw(trees())
    site = data.draw(drafts(base))
    with tempfile.TemporaryDirectory() as tmp:
        repo, content_hash = _absorb_into(base, site, Path(tmp))
        assert _hash_without_absorbs(repo) == content_hash


@settings(max_examples=1000, deadline=None)
@given(st.data())
def test_absorb_keeps_tags_the_repo_added(data: st.DataObject) -> None:
    base = data.draw(trees())
    site = data.draw(drafts(base))
    rel = data.draw(st.sampled_from(sorted(r for r in base if r.startswith("tags/"))))
    device = rel.removeprefix("tags/").removesuffix(".yaml")
    added = [
        data.draw(_tag(f"{device}/new{i}", device)) for i in range(data.draw(st.integers(1, 3)))
    ]

    def with_added(tree: Tree) -> Tree:
        tags = tree[rel]
        assert isinstance(tags, list)
        return {**tree, rel: [*tags, *added]}

    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = _absorb_into(with_added(base), site, Path(tmp))
        expected = _loaded(_write(Path(tmp) / "expected", with_added(site)))
        assert _hash_without_absorbs(repo) == expected.content_hash()
