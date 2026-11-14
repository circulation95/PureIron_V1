"""fleet GitHub 조작(M6-4a A1) — 무인 토큰 경로(★D5·P7), merge 전 머리 승인 확인, E_APPROVAL 전용
merge, publish 로그 해석. GitHub은 `fleet_fakes.FakeGitHub`(메모리)다 — 실제 `gh`는 부르지
않는다."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest
from fleet_fakes import TOKENS, FakeGitHub, repo, site_yaml
from harness import fleet
from harness.context import InfraError
from harness.fleet import FleetError, Gh, NotFoundError

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


# ---------------------------------------------------------------- 토큰(★D5·P7)


def test_tokens_never_reach_argv_and_only_ride_the_child_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """토큰은 부를 때마다 `gh auth token -u`로 읽고 그 `gh` 자식의 `GH_TOKEN`에만 —
    인자·stdin·출력에 없다. 바깥 `GH_TOKEN`·`GITHUB_TOKEN`은 자식에 넘기지 않는다."""
    monkeypatch.setenv("GH_TOKEN", "outer-actions-token")
    gh = seeded(site_a=site_yaml("site-a", 2))
    r = fleet.Repo(Gh(gh), R, sleep=lambda s: None, clock=lambda: 0.0)
    pull = r.open_pull(*(_branch(r), "[g2-nightly R] x", "body"))
    r.approve(pull.number, pull.head, "ok")
    r.merge(pull.number)
    r.set_secret("PUREIRON_FLEET_VIEWER_TOKEN", "viewer-SECRET")
    secrets = (*TOKENS.values(), "outer-actions-token")
    assert not any(s in gh.argv_and_stdin() for s in TOKENS.values())
    api_calls = [c for c in gh.calls if c.args[0] != "auth"]
    assert api_calls
    for call in api_calls:
        assert call.env["GH_TOKEN"] in TOKENS.values()
        assert "GITHUB_TOKEN" not in call.env
    auth = [c for c in gh.calls if c.args[0] == "auth"]
    assert all("GH_TOKEN" not in c.env for c in auth)
    printed = capsys.readouterr()
    assert not any(s in printed.out + printed.err for s in secrets)
    # 작성은 ws-blip, 승인·merge는 circulation95
    by = {tuple(c.args[2:4]): c.env["GH_TOKEN"] for c in api_calls}
    assert by[("POST", f"repos/{R}/pulls")] == TOKENS["ws-blip"]
    assert by[("PUT", f"repos/{R}/pulls/1/merge")] == TOKENS["circulation95"]
    # secret 값은 stdin으로만, 끝 개행 없이
    assert gh.secrets[(R, "PUREIRON_FLEET_VIEWER_TOKEN")] == "viewer-SECRET"


def _branch(r: fleet.Repo) -> str:
    r.create_branch("g2/R-x", r.main_sha())
    r.put_file("g2/R-x", "sites/site-a.yaml", site_yaml("site-a", 3), "m")
    return "g2/R-x"


def test_a_failing_call_never_leaks_the_token_into_the_exception() -> None:
    """gh가 실패하며 stderr에 토큰을 찍어도 예외 문자열에는 지운 값만 남는다. 실패는 infra."""
    gh = seeded(site_a=site_yaml("site-a", 2))
    gh.fail["pulls/1/merge"] = f"boom {TOKENS['circulation95']} (HTTP 500)"
    r, pull = open_pr(gh)
    r.approve(pull.number, pull.head, "ok")
    with pytest.raises(FleetError) as raised:
        r.merge(pull.number)
    assert TOKENS["circulation95"] not in str(raised.value)
    assert "***" in str(raised.value)
    assert isinstance(raised.value, InfraError)


def test_a_failed_token_read_shows_neither_stdout_nor_the_token() -> None:
    calls: list[list[str]] = []

    def run(
        args: Sequence[str], stdin: str | None, env: Mapping[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess[str]([], 1, "half-token-XYZ", "locked half-token-XYZ")

    with pytest.raises(FleetError) as raised:
        Gh(run).call("circulation95", ["api", "-X", "GET", "user"])
    assert "half-token-XYZ" not in str(raised.value)
    assert calls == [["auth", "token", "-u", "circulation95"]]


# ---------------------------------------------------------------- merge 전 승인 확인(P7)


def test_merge_without_a_head_approval_never_calls_merge() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, pull = open_pr(gh)
    with pytest.raises(FleetError, match="no circulation95 approval"):
        r.merge(pull.number)
    assert gh.writes("PUT", "/merge") == []


def test_a_stale_approval_is_not_an_approval() -> None:
    """승인 뒤 커밋이 붙으면(commit_id != head) 그 승인은 무효 — merge하지 않는다."""
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, pull = open_pr(gh)
    r.approve(pull.number, pull.head, "ok")
    r.put_file("g2/R-x", "sites/site-a.yaml", site_yaml("site-a", 4), "later")
    gh.pulls[(R, 1)]["head"] = {"sha": gh.refs[(R, "heads/g2/R-x")]}
    with pytest.raises(FleetError):
        r.merge(pull.number)
    assert gh.writes("PUT", "/merge") == []


@pytest.mark.parametrize(
    ("reviews", "ok"),
    [
        ([("circulation95", "APPROVED", "H")], True),
        ([("Circulation95", "APPROVED", "H")], True),  # login은 대소문자 무시
        ([("circulation95", "APPROVED", "OLD")], False),  # 오래된 승인
        ([("ws-blip", "APPROVED", "H")], False),  # 다른 사람
        ([("circulation95", "APPROVED", "H"), ("circulation95", "CHANGES_REQUESTED", "H")], False),
        ([("circulation95", "APPROVED", "H"), ("circulation95", "COMMENTED", "H")], True),
        ([("circulation95", "DISMISSED", "H")], False),
        ([], False),
    ],
)
def test_approved_at_head_follows_the_publish_rule(
    reviews: list[tuple[str, str, str]], ok: bool
) -> None:
    rows = [{"user": {"login": u}, "state": s, "commit_id": c} for u, s, c in reviews]
    assert fleet.approved_at_head(rows, "H", "circulation95") is ok
    assert fleet.approved_at_head(rows, "", "circulation95") is False


def test_merge_with_a_head_approval_squash_merges_that_head() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, pull = open_pr(gh)
    r.approve(pull.number, pull.head, "ok")
    sha = r.merge(pull.number)
    assert gh.refs[(R, "heads/main")] == sha
    (call,) = gh.writes("PUT", "/merge")
    assert json.loads(call.stdin or "{}") == {"merge_method": "squash", "sha": pull.head}


def test_unapproved_merge_is_only_for_the_e_approval_probe() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    r, pull = open_pr(gh)
    with pytest.raises(FleetError, match="not an E_APPROVAL probe"):
        r.merge_unapproved(pull)
    assert gh.writes("PUT", "/merge") == []
    r.create_branch("g2/R-p", r.main_sha())
    probe = r.open_probe("g2/R-p", "[g2-nightly R] E_APPROVAL", "b")
    r.approve(probe.number, probe.head, "oops")
    with pytest.raises(FleetError, match="has an approval"):
        r.merge_unapproved(probe)
    gh.reviews[(R, probe.number)] = []
    assert r.merge_unapproved(probe) == gh.refs[(R, "heads/main")]


def test_waits_time_out_as_infra() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    now = [0.0]

    def clock() -> float:
        now[0] += 100.0
        return now[0]

    r = fleet.Repo(Gh(gh, environ={}), R, sleep=lambda s: None, clock=clock)
    with pytest.raises(FleetError, match="not seen within"):
        r.wait_new_run(fleet.PUBLISH_WORKFLOW, "f" * 40, set())


def test_a_404_is_its_own_error() -> None:
    gh = seeded(site_a=site_yaml("site-a", 2))
    with pytest.raises(NotFoundError):
        repo(gh).gh.api("circulation95", "GET", f"repos/{R}/git/ref/heads/nope-really")
    assert repo(gh).ref_sha("nope") is None


# ---------------------------------------------------------------- publish 로그


STEP = "publish\tUNKNOWN STEP\t2026-11-15T00:00:01Z "
LOG = "\n".join(
    STEP + line
    for line in (
        "::notice::site-a gen 7 게시(pureiron-site-a-g7.pib, 1 bytes)",
        "::warning file=sites/site-c.yaml::site-c gen 3은 이미 게시됨 — 건너뜀(같은 커밋…)",
        "::notice::게시본이 최신인 현장(변경 없음): site-b, site-d",
        "publish: 게시 1, 건너뜀 1, 최신 2, 실패 0, 규칙 위반 0",
    )
)


def test_publish_outcome_reads_the_summary_and_per_site_lines() -> None:
    out = fleet.publish_outcome(LOG)
    assert out.counts == {"published": 1, "skipped": 1, "current": 2, "failed": 0, "violations": 0}
    assert out.published == {"site-a": 7}
    assert out.skipped == {"site-c": 3}
    assert out.current == {"site-b", "site-d"}
    assert out.e_approval is False


def test_a_rejected_run_has_no_summary() -> None:
    out = fleet.publish_outcome("::error::E_APPROVAL PR #3 유효 승인 0건(리뷰 없음)\n")
    assert (out.counts, out.e_approval, out.published) == (None, True, {})
