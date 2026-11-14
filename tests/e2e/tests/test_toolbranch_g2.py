"""fleet 도구 브랜치(M6-4a A3·★P2) — HEAD + NEXT·FAULT 기록 커밋, force 갱신, 토큰 경로."""

from __future__ import annotations

import json

import pytest
from fleet_fakes import TOKENS, FakeGitHub, repo, site_yaml
from harness import fleet, toolbranch
from harness.context import E2EError

R = fleet.FLEET_REPO


def seeded(**files: bytes) -> FakeGitHub:
    gh = FakeGitHub()
    gh.seed(R, {f"sites/{k.replace('_', '-')}.yaml": v for k, v in files.items()})
    return gh


def open_pr(gh: FakeGitHub, title: str = "[g2-nightly R] x") -> tuple[fleet.Repo, fleet.Pull]:
    r = repo(gh)
    r.create_branch("g2/R-x", r.main_sha())
    r.put_file("g2/R-x", "sites/site-a.yaml", site_yaml("site-a", 3), "m")
    return r, r.open_pull("g2/R-x", title, "body")


# ---------------------------------------------------------------- 도구 브랜치(A3)


def test_tool_branch_is_head_plus_the_records_in_repo_format() -> None:
    gh = FakeGitHub()
    head = gh.seed(fleet.V1_REPO, {"README.md": b"x", "deploy/releases/r5.json": b"r5"})
    v1 = repo(gh, fleet.V1_REPO)
    records = {"n1a2b3c4": {"name": "n1a2b3c4", "version": "2026.11.2-nightly.1a2b3c4"}}
    done = toolbranch.update_tool_branch(v1, head, records, "20261115T181700Z")
    tip = gh.refs[(fleet.V1_REPO, "heads/e2e/nightly-tool")]
    assert done["commit"] == tip
    assert gh.commits[tip]["parents"] == [head]
    files = gh.files_at(fleet.V1_REPO, tip)
    assert files["README.md"] == b"x"  # HEAD 트리 그대로
    assert (
        files["deploy/releases/n1a2b3c4.json"]
        == (json.dumps(records["n1a2b3c4"], indent=2, sort_keys=True) + "\n").encode()
    )
    # force 갱신(이미 있으면 PATCH force=true) — 다음 run도 같은 이름
    again = toolbranch.update_tool_branch(v1, head, records, "20261116T181700Z")
    patched = gh.writes("PATCH", "git/refs/heads/e2e/nightly-tool")
    assert patched
    assert json.loads(patched[-1].stdin or "{}")["force"] is True
    assert gh.refs[(fleet.V1_REPO, "heads/e2e/nightly-tool")] == again["commit"]
    assert all(c.env["GH_TOKEN"] == TOKENS["circulation95"] for c in gh.calls if c.args[0] == "api")
    assert not any(t in gh.argv_and_stdin() for t in TOKENS.values())


def test_tool_branch_needs_the_records() -> None:
    with pytest.raises(E2EError):
        toolbranch.update_tool_branch(repo(FakeGitHub()), "a" * 40, {}, "20261115T181700Z")
