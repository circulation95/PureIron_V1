"""고객 project repo 템플릿이 그대로 검증·정규형·결정적 hash를 통과한다."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pureiron_project.cli import main
from pureiron_project.project import load_project

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "pureiron-project"


def test_template_validates_and_is_canonical(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(TEMPLATE)]) == 0
    assert main(["fmt", "--check", str(TEMPLATE)]) == 0
    assert main(["hash", str(TEMPLATE)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("sha256:")


def test_template_content_is_only_project_files() -> None:
    # README·.github/·screens/.gitkeep·assets/.gitkeep는 프로젝트 내용이 아니다(hash에 안 들어간다).
    project = load_project(TEMPLATE)
    assert project.ok, project.problems
    assert sorted(f.relpath for f in project.files) == [
        "alarms/line1.yaml",
        "devices/plc-1.yaml",
        "project.yaml",
        "tags/plc-1.yaml",
    ]
    for keep in ("screens/.gitkeep", "assets/.gitkeep", ".github/workflows/validate.yml"):
        assert (TEMPLATE / keep).is_file()


def test_template_hash_is_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    copy = tmp_path / "pureiron-project-copy"
    shutil.copytree(TEMPLATE, copy)
    (copy / "README.md").write_text("고객이 고친 README", encoding="utf-8")
    hashes: list[str] = []
    for root in (TEMPLATE, TEMPLATE, copy):
        assert main(["hash", str(root)]) == 0
        hashes.append(capsys.readouterr().out.splitlines()[-1])
    assert len(set(hashes)) == 1
    assert load_project(TEMPLATE).content_hash() == hashes[0]


def test_template_workflow_calls_reusable_validate_project() -> None:
    workflow = (TEMPLATE / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    assert "uses: circulation95/PureIron_V1/.github/workflows/validate-project.yml@" in workflow
    for secret in ("PUREIRON_CI_APP_ID", "PUREIRON_CI_APP_KEY"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in workflow
