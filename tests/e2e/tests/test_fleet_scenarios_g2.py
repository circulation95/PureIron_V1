"""fleet 시나리오(M6-4a A6, ★P5) — G2-2 예약 갱신·G2-3 격리의 판정(순수 함수)과 시나리오 흐름
(가짜 중앙·VM, 중앙 직접 PUT). 변이 대상: verified < 창 시작, locked→succeeded ≤ 120초, 실행 이미지
== NEXT, G2-3 비교 항목·중앙 site-b 불변, 게시 gen."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fleet_fakes import FakeRemote
from g2_fakes import digests, scenario_session
from harness import fleet_scenarios, fleet_session, g2, judges
from harness.context import Context, E2EError, InfraError
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


B_ROW: dict[str, object] = {"site": "site-b", "desired": None, "max_published_gen": 0}


def test_g2_3_central_row_of_site_b_must_not_move() -> None:
    """변이: 중앙 site-b desired 불변을 빼면 site-a 게시가 site-b에도 게시본을 남겨도(VM은 아직
    받지 않아 그대로) 통과한다."""
    assert all(judges.judge_central_unchanged(B_ROW, B_ROW).values())
    moved = {**B_ROW, "desired": {"generation": 2}}
    assert scenarios_failed(judges.judge_central_unchanged(B_ROW, moved)) == [
        "central_desired_unchanged"
    ]
    bumped = {**B_ROW, "max_published_gen": 2}
    assert scenarios_failed(judges.judge_central_unchanged(B_ROW, bumped)) == [
        "central_max_unchanged"
    ]
    # 행을 못 읽었으면(빈 행) 둘 다 비어 같아도 통과가 아니다
    assert not any(judges.judge_central_unchanged({}, {}).values())
    assert not any(judges.judge_central_unchanged(B_ROW, {}).values())


def test_g2_2_publish_needs_201_and_the_next_desired() -> None:
    row: dict[str, object] = {
        "desired": {"generation": 2, "product_version": "2026.11.2-nightly.aaaaaaa"},
        "max_published_gen": 2,
    }

    def judged(put: dict[str, object], r: dict[str, object]) -> dict[str, bool]:
        return judges.judge_published(put, r, gen=2, next_version="2026.11.2-nightly.aaaaaaa")

    assert all(judged({"status": 201}, row).values())
    assert scenarios_failed(judged({"status": 409}, row)) == ["put_201"]
    assert scenarios_failed(judged({"status": 201}, {**row, "max_published_gen": 3})) == [
        "desired_next"
    ]
    old = {**row, "desired": {"generation": 2, "product_version": "2026.11.1"}}
    assert scenarios_failed(judged({"status": 201}, old)) == ["desired_next"]


@pytest.mark.parametrize(
    ("central_max", "applied", "gen"),
    [
        (0, 1, 2),  # c1: 중앙 게시 없음, 현장 c0 g1 — max + 1 = 1은 같은 gen(no-op)
        (None, 1, 2),
        (5, 1, 6),  # 중앙이 앞서 있으면 중앙 max + 1
        (5, 7, 8),
    ],
)
def test_publish_gen_is_above_both_the_central_and_the_site(
    central_max: object, applied: object, gen: int
) -> None:
    """변이: 게시 gen을 고정값으로 두면 중앙이 앞서 있을 때 PUT이 거부되거나(낮은 gen) 현장이
    no-op으로 넘긴다."""
    assert fleet_scenarios.publish_gen(central_max, applied) == gen


# ---------------------------------------------------------------- 시나리오 흐름


def fleet_state(s: g2.Session) -> fleet_session.FleetSession:
    return fleet_session.FleetSession(
        run_id=s.run_id,
        c1_name="c1-x",
        ca=Path("ca"),
        ca_sha256="AB",
        c1_fleet={"site-a": {"max_published_gen": 0}, "site-b": {"max_published_gen": 0}},
        c1_nft={"site-a": {}, "site-b": {}},
    )


@dataclass
class Flow:
    session: g2.Session
    fake: FakeRemote
    scenario: fleet_scenarios.Scenario
    restores: list[str] = field(default_factory=list[str])
    builds: list[tuple[str, int, str, tuple[datetime, datetime], Path]] = field(
        default_factory=list[tuple[str, int, str, tuple[datetime, datetime], Path]]
    )


def flow(tmp_path: Path) -> Flow:
    s = scenario_session(tmp_path)
    s.pair = dataclasses.replace(
        s.pair, releases={**s.pair.releases, s.pair.next: "2026.11.2-nightly.aaaaaaa"}
    )
    for run in s.sites.values():
        run.ctx.digests = {**run.ctx.digests, s.pair.next: NEXT}
    fake = FakeRemote(s, now=START - timedelta(seconds=fleet_scenarios.WINDOW_LEAD_S))
    out = Flow(s, fake, fleet_scenarios.Scenario(fake.remote()))

    def restore(
        session: g2.Session, fs: object, sites: tuple[str, ...], **_: object
    ) -> dict[str, object]:
        out.restores.append(",".join(sites))
        return {}

    def build(
        ctx: Context, gen: int, release: str, window: tuple[datetime, datetime], tree: Path
    ) -> Path:
        out.builds.append((ctx.site.site_id, gen, release, window, tree))
        return tmp_path / f"pureiron-{ctx.site.site_id}-g{gen}-{release}.pib"

    def probe(host: Vm) -> dict[str, object]:
        """site-a는 수렴 전 c0 g1, site-b는 전후 같은 스냅샷(갱신되지 않았다)."""
        site = next(k for k, r in s.sites.items() if r.ctx.site.ip == host.ip)
        return snap(gen=fake.current.get(site, 1))

    def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        fake.current["site-a"] = gen
        fake.fleet_rows["site-a"] = {
            "current": {"gen": gen, "product": s.pair.releases[s.pair.next]}
        }
        return snap(gen), 30.0

    remote = dataclasses.replace(fake.remote(), probe=probe, converge=converge)
    out.scenario = fleet_scenarios.Scenario(
        remote, restore=restore, build=build, report=lambda h, pib: report()
    )
    return out


def test_scheduled_flow_publishes_directly_and_judges_g2_2_and_g2_3(tmp_path: Path) -> None:
    f = flow(tmp_path)
    s = f.session
    out = fleet_scenarios.scheduled(s, fleet_state(s), f.scenario)
    assert f.restores == ["site-a,site-b"]
    assert all(out.g2_2.values()), out.g2_2
    assert all(out.g2_3.values()), out.g2_3
    # 게시 gen = max(중앙 0, 현장 1) + 1, 창 = 번들을 만들기 직전 now + 2분 ~ +1시간
    assert f.builds == [
        ("site-a", 2, s.pair.next, (START, START + timedelta(hours=1)), s.tree_of(s.pair.next))
    ]
    puts = [e for e in f.fake.events if e.startswith("central put")]
    assert puts == ["central put --site site-a"]
    assert f.fake.published["site-a"]["max_published_gen"] == 2
    assert "site-b" not in f.fake.published


def test_a_site_b_that_changes_during_the_site_a_update_fails_g2_3(tmp_path: Path) -> None:
    """리뷰 M2: site-a 갱신 도중 site-b 값이 바뀌면(여기서는 applied_gen) G2-3이 흐름 수준에서
    거짓이다 — 전 스냅샷을 PUT 앞에서 찍고 뒤 스냅샷과 비교하는 배선을 고정한다. 변이: 전 스냅샷
    대신 뒤 스냅샷끼리 비교하면 늘 참이다."""
    f = flow(tmp_path)
    converge = f.scenario.remote.converge

    def converge_and_touch_b(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        f.fake.current["site-b"] = 3  # site-b가 (잘못) 갱신됐다
        return converge(host, gen)

    remote = dataclasses.replace(f.scenario.remote, converge=converge_and_touch_b)
    sc = dataclasses.replace(f.scenario, remote=remote)
    out = fleet_scenarios.scheduled(f.session, fleet_state(f.session), sc)
    assert scenarios_failed(out.g2_3) == ["applied_gen_unchanged"]
    assert all(out.g2_2.values()), out.g2_2


def test_the_window_start_is_whole_seconds_like_the_bundle(tmp_path: Path) -> None:
    """리뷰 H1: 번들 `window.once.start`는 초 단위로 적힌다. 지금 시각에 마이크로초가 있어도 판정의
    창 시작이 같은 초여야 한다 — 창이 열린 직후 같은 초 안의 lock(창 시작 + 0.1초)이 "창 전"이 되지
    않게. 변이: 절삭을 빼면 판정 창 시작이 +0.6초가 되어 `locked_in_window`가 거짓이다."""
    f = flow(tmp_path)
    f.fake.now = START - timedelta(seconds=fleet_scenarios.WINDOW_LEAD_S - 0.6)
    sc = dataclasses.replace(
        f.scenario, report=lambda h, pib: report(locked=at(0.1), succeeded=at(60))
    )
    out = fleet_scenarios.scheduled(f.session, fleet_state(f.session), sc)
    assert f.builds[0][3][0] == START
    assert out.g2_2["locked_in_window"] is True, out.g2_2


def test_the_put_gen_follows_the_central_max(tmp_path: Path) -> None:
    """중앙이 앞서 있으면(max 5) 게시 gen은 6이다 — 고정값이 아니다."""
    f = flow(tmp_path)
    f.fake.fleet_rows["site-a"] = {"max_published_gen": 5}
    out = fleet_scenarios.scheduled(f.session, fleet_state(f.session), f.scenario)
    assert f.builds[0][1] == 6
    assert f.fake.published["site-a"]["max_published_gen"] == 6
    assert out.g2_2["put_201"]


def test_a_put_that_is_refused_is_a_product_failure(tmp_path: Path) -> None:
    f = flow(tmp_path)
    f.fake.put_status = 409
    with pytest.raises(E2EError, match="PUT /bundles/site-a/2 -> 409"):
        fleet_scenarios.scheduled(f.session, fleet_state(f.session), f.scenario)


def test_a_put_that_ends_too_close_to_the_window_is_infra(tmp_path: Path) -> None:
    f = flow(tmp_path)
    lead = timedelta(seconds=fleet_scenarios.WINDOW_LEAD_S)
    clock = iter([START - lead, START - timedelta(seconds=10)])
    remote = dataclasses.replace(f.scenario.remote, now=lambda: next(clock))
    sc = dataclasses.replace(f.scenario, remote=remote)
    with pytest.raises(InfraError, match="too close to the window start"):
        fleet_scenarios.scheduled(f.session, fleet_state(f.session), sc)
    assert not any(e.startswith("converge") for e in f.fake.events)


def test_the_g2_test_items_are_exactly_the_judged_keys() -> None:
    """시험 파일의 항목 목록(parametrize)과 판정 함수가 내는 키가 같다 — 판정 하나를 빼면(또는 키
    이름을 바꾸면) 그 시험 항목이 조용히 사라지거나 늘 실패하지 않게."""
    view = judges.isolation_view(snap(gen=1))
    g2_3 = {**judges.judge_isolation(view, view), **judges.judge_central_unchanged(B_ROW, B_ROW)}
    assert tuple(g2_3) == fleet_scenarios.G2_3_ITEMS
    published = judges.judge_published({}, {}, gen=2, next_version="x")
    assert (*published, *scheduled(report())) == fleet_scenarios.G2_2_ITEMS
