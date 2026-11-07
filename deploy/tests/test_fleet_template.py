"""fleet repo 템플릿(F9)과 reusable workflow(F6·C11) 고정값."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from fleet.approval import parse_approvers
from fleet.fleetfiles import active_sites, diff_trees, parse_site, structure

from pureiron_project.canonical import Json, load

DEPLOY = Path(__file__).resolve().parents[1]
REPO = DEPLOY.parent
TEMPLATE = DEPLOY / "templates" / "pureiron-fleet"
WORKFLOWS = REPO / ".github" / "workflows"
CALL = re.compile(r"uses: circulation95/PureIron_V1/\.github/workflows/(fleet-[a-z-]+\.yml)@(\S+)")


def tree() -> dict[str, bytes]:
    return {
        p.relative_to(TEMPLATE).as_posix(): p.read_bytes()
        for p in sorted(TEMPLATE.rglob("*"))
        if p.is_file()
    }


def workflow(path: Path) -> dict[str, Json]:
    return cast(dict[str, Json], load(path.read_text(encoding="utf-8"), path.name))


def test_template_layout() -> None:
    assert sorted(tree()) == [
        ".github/workflows/check-gen.yml",
        ".github/workflows/publish.yml",
        "CODEOWNERS",
        "README.md",
        "allowed_approvers.txt",
        "examples/site.yaml",
        "sites/.retired/.gitkeep",
    ]
    # 견본은 sites/ 밖이어야 한다 — sites/의 활성 파일은 모두 게시 대상(정합화, 리뷰 r2 R2-1).
    sites, broken = active_sites(tree())
    assert (sites, broken) == ([], [])


def test_example_site_passes_fleet_rules() -> None:
    files = tree()
    example = files["examples/site.yaml"]
    site, problems = parse_site(example, "examples/site.yaml")
    assert site is not None, problems
    assert (site.site, site.generation, site.writes_enabled) == ("example", 1, False)
    # README 순서대로 sites/<site>.yaml로 복사하면 check-gen 구조 규칙을 통과한다.
    copied = {**files, "sites/example.yaml": example}
    updates, findings = structure(diff_trees(files, copied), copied)
    assert findings == []
    assert [u.site.site for u in updates] == ["example"]
    text = example.decode()
    assert "# writes: { enabled: true }" in text  # 쓰기 스위치 예시(주석)
    assert "Updater 교체 전에" in text  # M4-1 이관: updater.env → 서명 스위치 업그레이드 안내


def test_approvers_file_parses() -> None:
    logins, problems = parse_approvers(tree()["allowed_approvers.txt"].decode())
    assert (logins, problems) == ({"circulation95"}, [])


def test_template_workflows_are_thin_callers_pinned_to_one_ref() -> None:
    calls: dict[str, str] = {}
    for name, target in (
        ("check-gen.yml", "fleet-check-gen.yml"),
        ("publish.yml", "fleet-publish.yml"),
    ):
        text = (TEMPLATE / ".github" / "workflows" / name).read_text(encoding="utf-8")
        found = CALL.findall(text)
        assert [f[0] for f in found] == [target]
        calls[name] = found[0][1]
        assert 'runs_on: \'["self-hosted", "publish"]\'' in text
        assert "run:" not in text  # 호출만 — 판정·서명 코드는 V1에
        for secret in ("PUREIRON_CI_APP_ID", "PUREIRON_CI_APP_KEY"):
            assert f"{secret}: ${{{{ secrets.{secret} }}}}" in text
    assert len(set(calls.values())) == 1
    check = workflow(TEMPLATE / ".github" / "workflows" / "check-gen.yml")
    assert check["on"] == {"pull_request": {"branches": ["main"]}}
    assert "PUREIRON_FLEET_VIEWER_TOKEN: ${{ secrets.PUREIRON_FLEET_VIEWER_TOKEN }}" in (
        TEMPLATE / ".github" / "workflows" / "check-gen.yml"
    ).read_text(encoding="utf-8")
    publish = workflow(TEMPLATE / ".github" / "workflows" / "publish.yml")
    assert publish["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": None}


def test_reusable_workflows_are_workflow_call_and_log_tool_commit() -> None:
    for name in ("fleet-check-gen.yml", "fleet-publish.yml", "validate-project.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        doc = workflow(WORKFLOWS / name)
        assert "workflow_call" in cast(dict[str, Json], doc["on"])
        assert "git -C pureiron-v1 rev-parse HEAD" in text  # C11: 도구 커밋 = checkout 결과
    check = (WORKFLOWS / "fleet-check-gen.yml").read_text(encoding="utf-8")
    assert "deploy/fleet/check_gen.py" in check
    assert "PUREIRON_FLEET_VIEWER_TOKEN: ${{ secrets.PUREIRON_FLEET_VIEWER_TOKEN }}" in check
    publish = (WORKFLOWS / "fleet-publish.yml").read_text(encoding="utf-8")
    assert "deploy/fleet/publish.py run" in publish
    assert '--sha "$SHA"' in publish
    assert (
        "path: fleet" not in publish
    )  # ★F1: fleet repo를 checkout하지 않는다(publish.py가 다시 받음)
    assert "concurrency" not in workflow(WORKFLOWS / "fleet-publish.yml")


def test_readme_documents_order_recovery_and_k14() -> None:
    readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    for needle in (
        "새 현장 순서 (등록 먼저)",
        "workflow_dispatch",
        "trust-base",
        "K14",
        "서명 경계를 실제로 분리하는 조건",
        "/etc/pureiron/publish/viewer_token",
        "pureiron-project gate --draft",
        "E_APPROVAL",
        "examples/site.yaml",
        "rebase merge를 끄고 squash만 허용",
        "①·②·⑤",
        "이 제외는 그 run만이다",
    ):
        assert needle in readme
    assert "sites/example.yaml`을" not in readme
