"""fleet-test 정리·사슬 대조·acme-test G2 태그·기준선 PR(M6-4a A5 — P2·P8·★P3)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fleet_fakes import FakeGitHub, FakeRemote, repo, repos, site_yaml
from g2_fakes import scenario_session, session
from harness import baseline, fleet, fleet_session, g2, judges
from harness.context import E2EError, InfraError

from pureiron_bundle.manifest import SchemaName, validate
from pureiron_project import canonical
from pureiron_project.project import load_project

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


# ---------------------------------------------------------------- 정리·사슬(A5·P8)


def test_a_human_pr_stops_the_session_before_any_write() -> None:
    """R5: fleet-test에 사람이 연 PR이 있으면 아무것도 쓰지 않고 건너뜀(infra)."""
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, _ = open_pr(gh, title="site-a 창 바꾸기")
    before = len(gh.calls)
    with pytest.raises(InfraError, match="not made by the nightly"):
        baseline.clear_leftovers(r)
    assert all(c.args[2] == "GET" for c in gh.calls[before:] if c.args[0] == "api")


def test_bot_leftovers_are_closed_and_their_branches_deleted() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, pull = open_pr(gh)
    done = baseline.clear_leftovers(r)
    assert done == {"closed": [pull.number], "deleted": ["g2/R-x"]}
    assert gh.pulls[(R, pull.number)]["state"] == "closed"
    assert (R, "heads/g2/R-x") not in gh.refs


def test_chain_matching_main_needs_nothing() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    fake = FakeRemote(scenario_session_stub(), chain=[gh.refs[(R, "heads/main")]])
    note = fleet_session.reconcile(repo(gh), fake.remote(), "R")
    assert note["action"] == "none"
    assert gh.writes("PATCH", "git/refs") == []


def test_an_unapproved_main_is_forced_back_to_the_chain_and_the_publish_run_awaited() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    last = gh.refs[(R, "heads/main")]
    r, pull = open_pr(gh)
    r.merge_unapproved(r.open_probe(_second_branch(r), "[g2-nightly R] p", "b"))
    fake = FakeRemote(scenario_session_stub(), chain=[last])
    note = fleet_session.reconcile(r, fake.remote(), "R")
    assert note["action"] == "reverted"
    assert gh.refs[(R, "heads/main")] == last
    assert note["revert_conclusion"] == "success"
    assert "host trust-base" not in fake.events
    assert pull.number


def _second_branch(r: fleet.Repo) -> str:
    r.create_branch("g2/R-y", r.main_sha())
    r.put_file("g2/R-y", "sites/site-a.yaml", site_yaml("site-a", 5), "m")
    return "g2/R-y"


def test_an_approved_main_missing_from_the_chain_is_trusted_with_a_reason() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    last = gh.refs[(R, "heads/main")]
    r, pull = open_pr(gh)
    r.approve(pull.number, pull.head, "ok")
    r.merge(pull.number)
    fake = FakeRemote(scenario_session_stub(), chain=[last])
    note = fleet_session.reconcile(r, fake.remote(), "R")
    assert note["action"] == "trust-base"
    assert "host trust-base" in fake.events
    assert gh.writes("PATCH", "git/refs") == []


def test_a_chain_still_broken_after_the_revert_falls_back_to_trust_base() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    last = gh.refs[(R, "heads/main")]
    r, _ = open_pr(gh)
    r.merge_unapproved(r.open_probe(_second_branch(r), "[g2-nightly R] p", "b"))
    fake = FakeRemote(scenario_session_stub(), chain=[last, "e" * 40])
    note = fleet_session.reconcile(r, fake.remote(), "R")
    assert note["action"] == "trust-base"


def test_no_chain_at_all_is_infra() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    with pytest.raises(InfraError, match="no approval chain"):
        fleet_session.reconcile(repo(gh), FakeRemote(scenario_session_stub()).remote(), "R")


def scenario_session_stub() -> g2.Session:
    return session(Path("unused"))


# ---------------------------------------------------------------- acme-test G2 태그(★P3)


def test_tag_tree_empties_absorbs_and_stays_a_valid_project(tmp_path: Path) -> None:
    s = scenario_session(tmp_path)
    tree = baseline.tag_tree(s.tree, tmp_path / "tag")
    doc = canonical.load((tree / "project.yaml").read_text("utf-8"), "project")
    assert isinstance(doc, dict)
    assert "absorbs" not in doc
    assert load_project(tree).ok
    assert "absorbs" in (s.tree / "project.yaml").read_text("utf-8")  # 원본은 그대로


def test_project_version_is_v1_plus_15_decimal_digits_of_the_hash() -> None:
    version = baseline.version_of("sha256:" + "f" * 12 + "0" * 52)
    assert version == f"v1{0xFFFFFFFFFFFF:015d}"
    assert baseline.version_of("sha256:" + "0" * 64) == "v1" + "0" * 15
    for v in (version, baseline.version_of("sha256:" + "0" * 64)):
        assert not validate(
            SchemaName.FLEET_SITE,
            canonical.load(site_yaml("site-a", 1).decode().replace("v1", v, 1), "x"),
        )


def test_the_project_tag_holds_only_the_g2_tree_on_g2_base(tmp_path: Path) -> None:
    s = scenario_session(tmp_path)
    tree = baseline.tag_tree(s.tree, tmp_path / "tag")
    gh = FakeGitHub()
    gh.seed(fleet.PROJECT_REPO, {"project.yaml": b"project_id: change-me\n", "README.md": b"r"})
    project = repo(gh, fleet.PROJECT_REPO)
    made = baseline.ensure_project_tag(project, tree, "R")
    assert made["created"] is True
    tag = str(made["version"])
    commit = gh.refs[(fleet.PROJECT_REPO, f"tags/{tag}")]
    assert gh.refs[(fleet.PROJECT_REPO, "heads/g2/base")] == commit
    assert set(gh.files_at(fleet.PROJECT_REPO, commit)) == set(baseline.tree_files(tree))
    assert gh.refs[(fleet.PROJECT_REPO, "heads/main")] != commit  # main은 그대로
    again = baseline.ensure_project_tag(project, tree, "R2")
    assert again == {"version": tag, "created": False, "commit": commit}


# ---------------------------------------------------------------- 기준선 PR(P2)


def test_baseline_pr_carries_both_sites_and_needs_both_published(tmp_path: Path) -> None:
    """site-a gen+1(BASE)·site-b(없으면 gen 1, NEXT), 같은 태그, 늘 열린 1회 창. 변이: site-b 파일을
    빠뜨리면 기준선이 site-b를 게시하지 않는다."""
    s = scenario_session(tmp_path)

    def publish(repo_name: str, sha: str, event: str) -> tuple[str, str]:
        files = gh.files_at(repo_name, sha)
        lines = [
            f"::notice::{p[6:-5]} gen {judges.as_int(_gen(files[p]))} 게시(x.pib, 1 bytes)"
            for p in sorted(files)
        ]
        return "success", "\n".join(
            [*lines, "publish: 게시 2, 건너뜀 0, 최신 0, 실패 0, 규칙 위반 0"]
        )

    gh = FakeGitHub(publish=publish)
    gh.seed(R, {"sites/site-a.yaml": site_yaml("site-a", 5)})
    fake = FakeRemote(s)
    gens, merged = baseline.baseline(s, repo(gh), "v1000000000000001", fake.now)
    assert gens == {"site-a": 6, "site-b": 1}
    files = gh.files_at(R, merged.sha)
    a = canonical.load(files["sites/site-a.yaml"].decode(), "a")
    b = canonical.load(files["sites/site-b.yaml"].decode(), "b")
    assert isinstance(a, dict)
    assert isinstance(b, dict)
    assert (a["product"], b["product"]) == (
        s.pair.releases[s.pair.base],
        s.pair.releases[s.pair.next],
    )
    assert (
        a["project"] == b["project"] == {"repo": fleet.PROJECT_NAME, "version": "v1000000000000001"}
    )
    assert a["window"] == {"once": {"start": "2026-11-15T02:00:00Z", "end": "2026-11-16T03:00:00Z"}}
    for doc in (a, b):
        assert not validate(SchemaName.FLEET_SITE, doc)
    assert str(gh.pulls[(R, 1)]["title"]).startswith("[g2-nightly 20261110T180000Z] ")
    assert str(cast(dict[str, object], gh.pulls[(R, 1)]["head"])["ref"]).startswith("g2/")


def _gen(data: bytes) -> object:
    doc = canonical.load(data.decode(), "x")
    return doc.get("generation") if isinstance(doc, dict) else None


def test_a_baseline_that_does_not_publish_both_sites_is_a_product_failure(tmp_path: Path) -> None:
    s = scenario_session(tmp_path)
    gh = FakeGitHub()  # 기본 publish 로그: site-a gen 2만
    gh.seed(R, {"sites/site-a.yaml": site_yaml("site-a", 1)})
    with pytest.raises(E2EError, match="published_site-b"):
        baseline.baseline(s, repo(gh), "v1", FakeRemote(s).now)


def test_a_failed_check_gen_is_a_product_failure(tmp_path: Path) -> None:
    s = scenario_session(tmp_path)
    gh = FakeGitHub(check_conclusion="failure")
    gh.seed(R, {"sites/site-a.yaml": site_yaml("site-a", 1)})
    with pytest.raises(E2EError, match="check-gen failure"):
        baseline.baseline(s, repo(gh), "v1", FakeRemote(s).now)


def test_repos_use_the_shared_names() -> None:
    gh = FakeGitHub()
    names = repos(gh)
    assert (names.fleet.name, names.project.name, names.v1.name) == (
        "circulation95/pureiron-fleet-test",
        "circulation95/pureiron-project-acme-test",
        "circulation95/PureIron_V1",
    )
