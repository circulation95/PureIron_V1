"""fleet 시나리오(M6-4a A6) — G2-2 예약 갱신·G2-3 격리·E_APPROVAL의 판정(순수 함수)과 시나리오 흐름
(가짜 GitHub·중앙·VM). 변이 대상: verified < 창 시작, locked→succeeded ≤ 120초, 실행 이미지 == NEXT,
G2-3 비교 항목, E_APPROVAL 문자열·max 불변·되돌림."""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fleet_fakes import FakeGitHub, FakeRemote, Publish, repos, site_yaml
from g2_fakes import digests, scenario_session
from harness import fleet, fleet_scenarios, fleet_session, g2, judges
from harness.context import InfraError
from harness.vm import Vm

START = datetime(2026, 11, 15, 3, 3, tzinfo=UTC)
NEXT = digests("n1a2b3c4")


def at(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def report(**timeline: str) -> dict[str, object]:
    base = {"fetched": at(-100), "verified": at(-90), "locked": at(5), "succeeded": at(65)}
    return {"result": "succeeded", "timeline": {**base, **timeline}}


def snap(gen: int = 7, images: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "updater": {"applied_gen": gen, "last_result": "succeeded", "attempt": None},
        "containers": {
            s: {"image_id": d, "running": True, "started_at": "2026-11-15T02:00:00Z"}
            for s, d in (images or NEXT).items()
        },
        "project": {"status": 200, "body": {"content_hash": "sha256:" + "c" * 64}},
    }


ROW: dict[str, object] = {"current": {"gen": 7, "product": "2026.11.2-nightly.aaaaaaa"}}


def scheduled(rep: dict[str, object], snapshot: dict[str, object] | None = None) -> dict[str, bool]:
    return judges.judge_scheduled(
        rep,
        snapshot or snap(),
        ROW,
        gen=7,
        window_start=START,
        next_digests=NEXT,
        next_version="2026.11.2-nightly.aaaaaaa",
    )


def test_g2_2_passes_when_verified_before_the_window_and_applied_quickly() -> None:
    assert all(scheduled(report()).values())


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        ({"verified": at(10)}, "verified_before_window"),  # 창이 열린 뒤에야 검증
        ({"verified": at(0)}, "verified_before_window"),  # 같은 시각은 "전"이 아니다
        ({"locked": at(-5)}, "locked_in_window"),  # 창 전에 잠금
        ({"succeeded": at(126)}, "locked_to_succeeded_120s"),  # 121초
        ({"succeeded": "not a time"}, "locked_to_succeeded_120s"),
    ],
)
def test_g2_2_timeline_checks(change: dict[str, str], broken: str) -> None:
    checks = scheduled(report(**change))
    assert scenarios_failed(checks) == [broken]


def scenarios_failed(checks: dict[str, bool]) -> list[str]:
    return sorted(k for k, ok in checks.items() if not ok)


def test_g2_2_missing_timeline_is_never_a_pass() -> None:
    checks = scheduled({"result": "succeeded"})
    assert not checks["verified_before_window"]
    assert not checks["locked_in_window"]
    assert not checks["locked_to_succeeded_120s"]


def test_g2_2_running_images_must_be_next() -> None:
    old = digests("r5")
    assert scenarios_failed(scheduled(report(), snap(images=old))) == ["images_next"]
    empty = judges.judge_scheduled(
        report(), snap(images={}), ROW, gen=7, window_start=START, next_digests={}, next_version="x"
    )
    assert not empty["images_next"]


def test_g2_2_fleet_current_and_applied_gen() -> None:
    assert "succeeded" in scenarios_failed(scheduled(report(), snap(gen=6)))
    row = judges.judge_scheduled(
        report(),
        snap(),
        {"current": {"gen": 6}},
        gen=7,
        window_start=START,
        next_digests=NEXT,
        next_version="2026.11.2-nightly.aaaaaaa",
    )
    assert scenarios_failed(row) == ["fleet_current"]


def test_publish_split_needs_site_a_published_and_site_b_untouched() -> None:
    def split(
        published: dict[str, int], current: frozenset[str], counts: dict[str, int] | None
    ) -> dict[str, bool]:
        return judges.judge_publish_split(
            "success", published, counts, current, {}, changed="site-a", gen=7, untouched="site-b"
        )

    counts = {"published": 1, "skipped": 0, "current": 1, "failed": 0, "violations": 0}
    assert all(split({"site-a": 7}, frozenset({"site-b"}), counts).values())
    assert not split({"site-a": 7, "site-b": 2}, frozenset(), counts)["untouched_site-b"]
    assert not split({"site-a": 7}, frozenset(), counts)["untouched_site-b"]  # 읽지 못함
    assert not split({"site-a": 7}, frozenset({"site-b"}), None)["publish_no_failure"]
    assert not split({"site-a": 6}, frozenset({"site-b"}), counts)["published_site-a"]


@pytest.mark.parametrize("key", judges.ISOLATION_KEYS)
def test_g2_3_each_compared_value_matters(key: str) -> None:
    """변이: 비교 항목 하나를 빼면 그 값만 바뀐 site-b를 놓친다."""
    before = judges.isolation_view(snap(gen=1))
    assert all(judges.judge_isolation(before, before).values())
    after = {**before, key: {"core": "other"} if key == "images" else "changed"}
    assert scenarios_failed(judges.judge_isolation(before, after)) == [f"{key}_unchanged"]


def test_g2_3_unread_values_are_not_unchanged() -> None:
    blank: dict[str, object] = {}
    for key in judges.ISOLATION_KEYS:
        blank[key] = None
    blank["images"] = {}
    assert not any(judges.judge_isolation(blank, blank).values())


def approval(change: dict[str, object]) -> dict[str, bool]:
    v = {**APPROVAL_OK, **change}
    return judges.judge_e_approval(
        run_conclusion=cast(str | None, v["run_conclusion"]),
        e_approval_logged=bool(v["e_approval_logged"]),
        signed=bool(v["signed"]),
        max_before=v["max_before"],
        max_after=v["max_after"],
        chain_before=cast(str | None, v["chain_before"]),
        chain_after=cast(str | None, v["chain_after"]),
        main_before=str(v["main_before"]),
        main_after=str(v["main_after"]),
        revert_conclusion=cast(str | None, v["revert_conclusion"]),
        revert_counts=cast(dict[str, int] | None, v["revert_counts"]),
        revert_e_approval=bool(v["revert_e_approval"]),
    )


APPROVAL_OK: dict[str, object] = {
    "run_conclusion": "failure",
    "e_approval_logged": True,
    "signed": False,
    "max_before": 6,
    "max_after": 6,
    "chain_before": "a" * 40,
    "chain_after": "a" * 40,
    "main_before": "b" * 40,
    "main_after": "b" * 40,
    "revert_conclusion": "success",
    "revert_counts": {"published": 0, "skipped": 0, "current": 2, "failed": 0, "violations": 0},
    "revert_e_approval": False,
}


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        ({"e_approval_logged": False}, "e_approval_logged"),  # 실패만 보고 E_APPROVAL을 무시
        ({"run_conclusion": "success"}, "run_failed"),
        ({"max_after": 7}, "max_published_unchanged"),
        ({"max_before": None, "max_after": None}, "max_published_unchanged"),
        ({"chain_after": "c" * 40}, "chain_unchanged"),
        ({"chain_before": None, "chain_after": None}, "chain_unchanged"),
        ({"signed": True}, "not_signed"),
        ({"main_after": "d" * 40}, "main_reverted"),
        ({"revert_conclusion": "failure"}, "revert_published_nothing"),
        ({"revert_counts": None}, "revert_published_nothing"),
        (
            {"revert_counts": {"published": 1, "failed": 0}},
            "revert_published_nothing",
        ),
        ({"revert_e_approval": True}, "revert_published_nothing"),
    ],
)
def test_e_approval_checks(change: dict[str, object], broken: str) -> None:
    assert all(approval({}).values())
    checks = approval(change)
    assert scenarios_failed(checks) == [broken]


# ---------------------------------------------------------------- 시나리오 흐름


def fleet_state(s: g2.Session) -> fleet_session.FleetSession:
    return fleet_session.FleetSession(
        run_id=s.run_id,
        c1_name="c1-x",
        ca=Path("ca"),
        ca_sha256="AB",
        project_version="v1000000000000001",
        baseline_sha="",
        gens={"site-a": 6, "site-b": 1},
        c1_fleet={"site-a": {"max_published_gen": 6}, "site-b": {"max_published_gen": 1}},
        c1_nft={"site-a": {}, "site-b": {}},
    )


def flow(
    tmp_path: Path, publish: Publish
) -> tuple[g2.Session, FakeGitHub, FakeRemote, fleet_scenarios.Scenario, list[str]]:
    s = scenario_session(tmp_path)
    for run in s.sites.values():
        run.ctx.digests = {**run.ctx.digests, s.pair.next: NEXT}
    gh = FakeGitHub(publish=publish)
    gh.seed(
        fleet.FLEET_REPO,
        {"sites/site-a.yaml": site_yaml("site-a", 6), "sites/site-b.yaml": site_yaml("site-b", 1)},
    )
    fake = FakeRemote(s, now=START - timedelta(seconds=fleet_scenarios.WINDOW_LEAD_S))
    restores: list[str] = []

    def restore(
        session: g2.Session, fs: object, sites: tuple[str, ...], **_: object
    ) -> dict[str, object]:
        restores.append(",".join(sites) or "central")
        return {}

    sc = fleet_scenarios.Scenario(
        fake.remote(), repos(gh), restore=restore, report=lambda h, sha: report()
    )
    return s, gh, fake, sc, restores


def scheduled_publish(repo: str, sha: str, event: str) -> tuple[str, str]:
    lines = [
        "::notice::site-a gen 7 게시(x.pib, 1 bytes)",
        "::notice::게시본이 최신인 현장(변경 없음): site-b",
        "publish: 게시 1, 건너뜀 0, 최신 1, 실패 0, 규칙 위반 0",
    ]
    return "success", "\n".join(lines)


def converged(host: Vm, gen: int) -> tuple[dict[str, object], float]:
    return snap(gen), 30.0


def site_b_probe(host: Vm) -> dict[str, object]:
    """site-b 전후 같은 스냅샷(갱신되지 않았다)."""
    return snap(gen=1)


def test_scheduled_flow_judges_g2_2_and_g2_3(tmp_path: Path) -> None:
    s, gh, fake, sc, restores = flow(tmp_path, scheduled_publish)
    sc = dataclasses.replace(
        sc, remote=dataclasses.replace(sc.remote, probe=site_b_probe, converge=converged)
    )
    current: dict[str, object] = {"gen": 7, "product": "2026.11.2-nightly.aaaaaaa"}
    fake.fleet_rows = {"site-a": {"desired": {"sha256": "sha256:" + "d" * 64}, "current": current}}
    s.pair = dataclasses.replace(
        s.pair, releases={**s.pair.releases, s.pair.next: "2026.11.2-nightly.aaaaaaa"}
    )
    out = fleet_scenarios.scheduled(s, fleet_state(s), sc)
    assert restores == ["site-a,site-b"]
    assert all(out.g2_2.values()), out.g2_2
    assert all(out.g2_3.values()), out.g2_3
    pulled = gh.pulls[(fleet.FLEET_REPO, 1)]
    assert str(pulled["title"]).startswith(f"[g2-nightly {s.run_id}] G2-2")
    content = gh.files_at(fleet.FLEET_REPO, gh.refs[(fleet.FLEET_REPO, "heads/main")])
    assert b'start: "2026-11-15T03:03:00Z"' in content["sites/site-a.yaml"]
    head = cast(dict[str, object], pulled["head"])["sha"]
    assert gh.reviews[(fleet.FLEET_REPO, 1)][0]["commit_id"] == head


def test_a_publish_that_ends_too_close_to_the_window_is_infra(tmp_path: Path) -> None:
    s, _, _, sc, _ = flow(tmp_path, scheduled_publish)
    clock = iter([START - timedelta(seconds=180), START - timedelta(seconds=10)])
    sc = dataclasses.replace(sc, remote=dataclasses.replace(sc.remote, now=lambda: next(clock)))
    with pytest.raises(InfraError, match="too close to the window start"):
        fleet_scenarios.scheduled(s, fleet_state(s), sc)


def test_e_approval_flow_merges_unapproved_then_reverts(tmp_path: Path) -> None:
    main: dict[str, str] = {}

    def publish(repo: str, sha: str, event: str) -> tuple[str, str]:
        if sha == main["before"]:
            return "success", "publish: 게시 0, 건너뜀 0, 최신 2, 실패 0, 규칙 위반 0"
        return "failure", "::error::E_APPROVAL PR #1 유효 승인 0건(리뷰 없음)\npublish: x 승인 거부"

    s, gh, fake, sc, restores = flow(tmp_path, publish)
    main["before"] = gh.refs[(fleet.FLEET_REPO, "heads/main")]
    fake.chain = ["a" * 40]
    fake.fleet_rows = {"site-a": {"max_published_gen": 6}}
    checks = fleet_scenarios.e_approval(s, fleet_state(s), sc)
    assert restores == ["central"]  # VM은 되돌리지 않는다
    assert all(checks.values()), checks
    assert gh.refs[(fleet.FLEET_REPO, "heads/main")] == main["before"]
    assert gh.reviews[(fleet.FLEET_REPO, 1)] == []  # 승인 없이 merge
    patched = gh.writes("PATCH", "git/refs/heads/main")
    assert len(patched) == 1  # 되돌림 — 변이: 생략하면 main_reverted가 거짓


def test_e_approval_without_the_revert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def publish(repo: str, sha: str, event: str) -> tuple[str, str]:
        return "failure", "::error::E_APPROVAL x"

    s, _, fake, sc, _ = flow(tmp_path, publish)
    fake.chain = ["a" * 40]
    fake.fleet_rows = {"site-a": {"max_published_gen": 6}}
    checks = fleet_scenarios.e_approval(s, fleet_state(s), sc)
    assert not checks["revert_published_nothing"]


def test_e_approval_flags_a_main_ahead_of_c1(tmp_path: Path) -> None:
    def publish(repo: str, sha: str, event: str) -> tuple[str, str]:
        return "success", "publish: 게시 0, 건너뜀 0, 최신 2, 실패 0, 규칙 위반 0"

    s, _, fake, sc, _ = flow(tmp_path, publish)
    fs = fleet_state(s)
    fs.c1_fleet["site-a"]["max_published_gen"] = 5
    fake.chain = ["a" * 40]
    checks = fleet_scenarios.e_approval(s, fs, sc)
    assert not checks["main_matches_c1"]


def test_reports_are_found_by_the_published_bundle_sha() -> None:
    class Host:
        def __init__(self) -> None:
            self.command = ""

        def run(
            self, command: str, *, check: bool = True, timeout_s: float = 600
        ) -> subprocess.CompletedProcess[str]:
            self.command = command
            return subprocess.CompletedProcess[str]([], 0, '{"result": "succeeded"}', "")

    host = Host()
    found = fleet_scenarios.report_for_sha(host, "sha256:" + "e" * 64)
    assert found == {"result": "succeeded"}
    assert "*-eeeeeeeeeeee.json" in host.command
    assert fleet_scenarios.report_for_sha(host, "short") == {}


def test_the_g2_test_items_are_exactly_the_judged_keys() -> None:
    """시험 파일의 항목 목록(parametrize)과 판정 함수가 내는 키가 같다 — 판정 하나를 빼면(또는 키
    이름을 바꾸면) 그 시험 항목이 조용히 사라지거나 늘 실패하지 않게."""
    view = judges.isolation_view(snap(gen=1))
    g2_3 = {**judges.judge_isolation(view, view), "not_published": True}
    assert tuple(g2_3) == fleet_scenarios.G2_3_ITEMS
    split = judges.judge_publish_split(
        "success", {}, None, frozenset(), {}, changed="site-a", gen=7, untouched="site-b"
    )
    assert (*scheduled(report()), *split) == fleet_scenarios.G2_2_ITEMS
    assert ("main_matches_c1", *approval({})) == fleet_scenarios.E_APPROVAL_ITEMS
