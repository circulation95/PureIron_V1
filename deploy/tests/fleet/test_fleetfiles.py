"""fleet 파일 규칙(`E_FLEET`): 스키마·파일명·`.retired` 이동·변경 목록·annotation."""

from __future__ import annotations

import pytest
from fleet.fleetfiles import (
    ChangeKind,
    ErrorCode,
    PathKind,
    SiteUpdate,
    active_sites,
    annotation,
    classify,
    diff_trees,
    error,
    gen_step,
    parse_site,
    structure,
    warning,
    workflow_command,
)

from .kit import site_yaml


def b(text: str) -> bytes:
    return text.encode("utf-8")


A1 = b(site_yaml("site-a", 1))
A2 = b(site_yaml("site-a", 2))


@pytest.mark.parametrize(
    ("path", "kind", "name"),
    [
        ("sites/site-a.yaml", PathKind.ACTIVE, "site-a"),
        ("sites/.retired/site-a.yaml", PathKind.RETIRED, "site-a"),
        ("sites/.retired/.gitkeep", PathKind.GITKEEP, None),
        ("sites/site-a.yml", PathKind.OTHER, None),
        ("sites/sub/site-a.yaml", PathKind.OTHER, None),
        ("sites/Site-A.yaml", PathKind.OTHER, None),
        ("README.md", PathKind.OTHER, None),
    ],
)
def test_classify(path: str, kind: PathKind, name: str | None) -> None:
    assert classify(path) == (kind, name)


def test_parse_site_reads_contract_fields() -> None:
    site, problems = parse_site(b(site_yaml("site-a", 3, writes=True)), "sites/site-a.yaml")
    assert problems == []
    assert site is not None
    assert (site.site, site.generation, site.product, site.project_repo, site.project_version) == (
        "site-a",
        3,
        "2026.11.0",
        "pureiron-project-acme",
        "v3",
    )
    assert site.writes_enabled


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"site: site-a\ngeneration: 1\n", "required"),
        (b(site_yaml("site-a", 0)), "minimum"),
        (b(site_yaml("site-a", 1) + "extra: 1\n"), "Additional properties"),
        (b"\xff\xfe", "not UTF-8"),
        (b"a: &x 1\nb: *x\n", "anchors"),
    ],
)
def test_parse_site_rejects(data: bytes, expected: str) -> None:
    site, problems = parse_site(data, "sites/site-a.yaml")
    assert site is None
    assert any(expected in p for p in problems), problems


def test_diff_trees_only_sites_and_sorted() -> None:
    base = {"sites/a.yaml": b"1", "sites/b.yaml": b"x", "README.md": b"r"}
    head = {"sites/a.yaml": b"2", "sites/c.yaml": b"y", "README.md": b"changed"}
    assert [(c.kind, c.path) for c in diff_trees(base, head)] == [
        (ChangeKind.MODIFIED, "sites/a.yaml"),
        (ChangeKind.DELETED, "sites/b.yaml"),
        (ChangeKind.ADDED, "sites/c.yaml"),
    ]


def run(base: dict[str, bytes], head: dict[str, bytes]) -> tuple[list[str], list[str]]:
    updates, findings = structure(diff_trees(base, head), head)
    return [u.site.site for u in updates], [f"{f.code} {f.path} {f.message}" for f in findings]


def test_added_and_modified_files_are_updates() -> None:
    updates, findings = run(
        {"sites/site-a.yaml": A1},
        {"sites/site-a.yaml": A2, "sites/site-b.yaml": b(site_yaml("site-b", 1))},
    )
    assert (updates, findings) == (["site-a", "site-b"], [])


def test_filename_must_equal_site() -> None:
    updates, findings = run({}, {"sites/site-b.yaml": A1})
    assert updates == []
    assert findings == [
        "E_FLEET sites/site-b.yaml 파일명 'site-b' != site 'site-a'(sites/<site>.yaml)"
    ]


def test_schema_violation_is_e_fleet() -> None:
    updates, findings = run({}, {"sites/site-a.yaml": b"site: site-a\n"})
    assert updates == []
    assert findings
    assert all(f.startswith("E_FLEET sites/site-a.yaml") for f in findings)


def test_retire_is_a_pure_move() -> None:
    base = {"sites/site-a.yaml": A1}
    head = {"sites/.retired/site-a.yaml": A1}
    assert run(base, head) == ([], [])


def test_delete_without_retire_is_rejected() -> None:
    _, findings = run({"sites/site-a.yaml": A1}, {})
    assert findings == [
        "E_FLEET sites/site-a.yaml 현장 파일 삭제 금지 — 퇴역은 sites/.retired/site-a.yaml로 "
        "같은 내용 그대로 이동만 한다"
    ]


def test_retire_with_changed_content_is_rejected() -> None:
    _, findings = run({"sites/site-a.yaml": A1}, {"sites/.retired/site-a.yaml": A2})
    assert len(findings) == 2  # 삭제(이동 아님) + 퇴역 파일(옮긴 것 아님)


def test_retired_files_are_immutable_and_names_not_reused() -> None:
    retired = {"sites/.retired/site-a.yaml": A1}
    _, modified = run(retired, {"sites/.retired/site-a.yaml": A2})
    assert modified == [
        "E_FLEET sites/.retired/site-a.yaml 퇴역 파일은 고치거나 지울 수 없다(이름 재사용 방지)"
    ]
    _, deleted = run(retired, {})
    assert len(deleted) == 1
    _, reused = run(retired, {**retired, "sites/site-a.yaml": A1})
    assert reused == [
        "E_FLEET sites/site-a.yaml 퇴역한 현장 이름 'site-a'은 다시 쓸 수 없다"
        "(sites/.retired/site-a.yaml)"
    ]


def test_other_files_under_sites_rejected_but_gitkeep_allowed() -> None:
    _, findings = run({}, {"sites/notes.txt": b"x", "sites/.retired/.gitkeep": b""})
    assert len(findings) == 1
    assert findings[0].startswith("E_FLEET sites/notes.txt")
    assert run({"sites/old.txt": b"x"}, {}) == ([], [])  # 엉뚱한 파일 정리(삭제)는 허용


def test_annotation_format_and_escaping() -> None:
    assert annotation(error(ErrorCode.GEN, "sites/a,b:c.yaml", "50%\nnext")) == (
        "::error file=sites/a%2Cb%3Ac.yaml::E_GEN 50%25%0Anext"
    )
    assert annotation(warning("", "주의")) == "::warning::주의"


def test_workflow_command_escapes_message() -> None:
    message = "a%b\n::add-mask::x\r"
    assert workflow_command("notice", message) == "::notice::a%25b%0A::add-mask::x%0D"


def _update(new: str, old: str | None) -> SiteUpdate:
    site, _ = parse_site(b(new), "sites/site-a.yaml")
    previous = None if old is None else parse_site(b(old), "sites/site-a.yaml")[0]
    assert site is not None
    return SiteUpdate("sites/site-a.yaml", site, previous, ())


def test_gen_step_is_exactly_plus_one() -> None:
    assert gen_step(_update(site_yaml("site-a", 2), site_yaml("site-a", 1))) is None
    assert gen_step(_update(site_yaml("site-a", 1), None)) is None
    same = gen_step(_update(site_yaml("site-a", 1, product="2026.11.1"), site_yaml("site-a", 1)))
    assert same is not None
    assert (same.code, same.message) == (
        ErrorCode.GEN,
        "generation은 정확히 +1: 1 → 2이어야 하는데 1",
    )
    broken = SiteUpdate(
        "sites/site-a.yaml", _update(site_yaml("site-a", 2), None).site, None, ("x",)
    )
    finding = gen_step(broken)
    assert finding is not None
    assert "해석할 수 없어" in finding.message


def test_active_sites_lists_every_active_file() -> None:
    head = {
        "sites/site-a.yaml": A1,
        "sites/site-b.yaml": b(site_yaml("site-b", 3)),
        "sites/site-c.yaml": b(site_yaml("site-x", 1)),
        "sites/site-d.yaml": b"not: [valid",
        "sites/.retired/site-r.yaml": b(site_yaml("site-r", 1)),
        "sites/.retired/.gitkeep": b"",
        "README.md": b"x",
    }
    sites, findings = active_sites(head)
    assert [(s.path, s.site.generation) for s in sites] == [
        ("sites/site-a.yaml", 1),
        ("sites/site-b.yaml", 3),
    ]
    assert {(f.code, f.path) for f in findings} == {
        (ErrorCode.FLEET, "sites/site-c.yaml"),
        (ErrorCode.FLEET, "sites/site-d.yaml"),
    }


def test_active_sites_excludes_reused_retired_name() -> None:
    """리뷰 r2 R2-2 ③: 퇴역 이름 재사용은 추가한 커밋뿐 아니라 트리에 있는 동안 매번 위반."""
    head = {
        "sites/site-a.yaml": b(site_yaml("site-a", 2)),
        "sites/.retired/site-a.yaml": A1,
        "sites/site-b.yaml": b(site_yaml("site-b", 1)),
    }
    sites, findings = active_sites(head)
    assert [s.path for s in sites] == ["sites/site-b.yaml"]
    assert [(f.code, f.path) for f in findings] == [(ErrorCode.FLEET, "sites/site-a.yaml")]
    assert "퇴역한 현장 이름 'site-a'" in findings[0].message
