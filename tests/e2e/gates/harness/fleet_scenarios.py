"""fleet 시나리오(M6-4a A6, ★P5 개정) — 중앙 경로. M6-3 계약(H6)을 따른다: 출발 체크포인트(c1 쌍,
`fleet_session.restore_c1`) 복원으로 시작하고, 다른 시나리오의 상태에 기대지 않으며, 판정은
`judges`의 순수 함수, 원격 호출은 `Scenario`로 받는다.

게시는 중앙 직접 PUT이다(★P5, `run.py online` ②와 같은 길): 서명 번들(`buildhost.build_site_bundle`
— G2 트리·NEXT, `window.once`는 시나리오가 정한다)을 ci 토큰 `PUT /bundles/{site}/{gen}`으로 올린다.
GitHub 승인 경로(PR → check-gen → 승인 → merge → publish, 승인 없는 merge의 `E_APPROVAL`)는 nightly
밖 — 릴리스 마감 fleet 리허설(손)에서 본다(tests/e2e/README.md `fleet 묶음`).

- `scheduled`(G2-2 + G2-3, site-a·site-b c1): site-b 스냅샷(VM 값 + 중앙 행) → site-a NEXT 번들
  (`window.once` = 번들을 만들기 직전 now + `WINDOW_LEAD_S` ~ +1시간) 빌드·PUT → 창 → 적용 수렴 →
  판정(`judges.judge_published`·`judge_scheduled`·`judge_isolation`·`judge_central_unchanged`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harness import buildhost, fleet_session, g2, golden, judges, site
from harness.context import TAIL_CHARS, Context, E2EError, InfraError
from harness.fleet_session import FleetSession, Remote
from harness.vm import Vm

SCHEDULED_SITE = "site-a"
ISOLATED_SITE = "site-b"
WINDOW_LEAD_S = 240
"""번들을 만들기 직전부터 창 시작까지 = 빌드 85 + PUT 20 + `PREFETCH_ALLOWANCE_S` 45 + 여유 90초.
근거(첫 VM 실측 run 43617245939, `ee1d8fc`): Windows 하네스 경로의 번들 빌드 `build_s` 73.6·82.6초
(번들을 `C:/PureIron_e2e/bundles`(WSL에서 `/mnt/c`)에 쓴다 — WSL 네이티브 경로에서 잰 23초와
다르다). 120초일 때 PUT 완료가 창 시작 -17초(두 세션 모두)였으니 빌드 + PUT ≈ 103초다. 그 안에
빌드·PUT이 끝나고 남은 시간에 Updater가 poll(10초)로 받아 검증(`verified`)까지 마쳐야 한다."""
WINDOW_LEN = timedelta(hours=1)
PREFETCH_ALLOWANCE_S = 45
"""PUT 뒤 VM이 받아 검증하기까지 잡는 몫(poll 10초 + 다운로드·검증 약 30초 — M4 마감 실측 시도
시작 → succeeded 24초). PUT이 창 시작 - 이 값보다 늦게 끝나면 `verified < 창 시작`을 볼 수 없어
`InfraError`(빌드 호스트가 느린 것 — 제품 판정이 아니다)."""
G2_2_ITEMS = (
    *("put_201", "desired_next"),
    *("verified_before_window", "locked_in_window", "locked_to_succeeded_120s", "succeeded"),
    *("images_next", "fleet_current"),
)
"""G2-2 판정 항목(시험 `test_g2_2_scheduled_update[…]`) — `judge_published` + `judge_scheduled`."""
G2_3_ITEMS = (
    *("images_unchanged", "content_hash_unchanged", "applied_gen_unchanged"),
    *("core_started_at_unchanged", "central_desired_unchanged", "central_max_unchanged"),
)
"""G2-3 판정 항목 — `judge_isolation` + `judge_central_unchanged`."""
ITEM_G2_2 = "G2-2 scheduled update (central PUT -> window -> applied)"
ITEM_G2_3 = "G2-3 isolation (site-b untouched by the site-a update)"

Build = Callable[[Context, int, str, tuple[datetime, datetime], Path], Path]


def build_bundle(
    ctx: Context, gen: int, release: str, window: tuple[datetime, datetime], tree: Path
) -> Path:
    """G2 현장 번들(nightly 작업 디렉터리의 기록·store, 트리 `tree`, `window.once` = `window`)."""
    return buildhost.build_site_bundle(
        ctx,
        gen,
        release,
        window[0],
        golden.wsl_path(tree),
        work=buildhost.NIGHTLY_WORK,
        window_close_at=window[1],
    )


def seconds_after(report: dict[str, object], phase: str, moment: datetime) -> float | None:
    """보고 `timeline`의 `phase` 시각 - `moment`(초, 소수 첫째 자리). 못 읽으면 None — 기록용(리뷰
    L1: 첫 실측에서 `PREFETCH_ALLOWANCE_S`를 다시 맞출 값)."""
    at = judges.timeline(report, phase)
    return round((at - moment).total_seconds(), 1) if at is not None else None


def publish_gen(central_max: object, applied: object) -> int:
    """게시할 gen = max(중앙 `max_published_gen`, 현장 applied gen) + 1. c1은 중앙 0·현장 1(c0 g1은
    폐쇄망 `apply`라 중앙에 없다)이라 중앙 max + 1 = 1은 Updater에게 같은 gen(no-op)이다 — `run.py
    online`이 `ONLINE_GEN = 2`를 쓰는 것과 같은 이유."""
    return max(judges.as_int(central_max), judges.as_int(applied)) + 1


@dataclass(frozen=True)
class Scenario:
    """fleet 시나리오의 조작 — 기본값이 실제 호출이다."""

    remote: Remote
    restore: Callable[..., dict[str, object]] = fleet_session.restore_c1
    build: Build = build_bundle
    report: Callable[[Vm, Path], dict[str, object]] = site.report_for

    @classmethod
    def default(cls) -> Scenario:
        return cls(Remote())


@dataclass(frozen=True)
class ScheduledOutcome:
    g2_2: dict[str, bool]
    g2_3: dict[str, bool]


def fleet_status(remote: Remote, site_id: str) -> dict[str, object]:
    return judges.fleet_row(fleet_session.central(remote, "fleet", "--site", site_id), site_id)


def put(remote: Remote, site_id: str, gen: int, pib: Path) -> dict[str, object]:
    """ci 토큰 `PUT /bundles/{site}/{gen}`(`central.py put`). WSL 실패는 `InfraError`, 201이 아니면
    제품(`E2EError` — 중앙이 서명 번들을 받지 않았다)."""
    done = fleet_session.central(
        remote, "put", "--site", site_id, "--gen", str(gen), "--pib", golden.wsl_path(pib)
    )
    if done.get("status") != 201:
        raise E2EError(f"PUT /bundles/{site_id}/{gen} -> {done.get('status')}: {done.get('body')}")
    return done


def scheduled(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> ScheduledOutcome:
    """G2-2 + G2-3(모듈 설명). 기록은 site-a·site-b `Context`에."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    restored = sc.restore(session, fs, (SCHEDULED_SITE, ISOLATED_SITE), remote=remote)
    a, b = g2.site_up(session, SCHEDULED_SITE), g2.site_up(session, ISOLATED_SITE)
    before_b = judges.isolation_view(remote.probe(b.ctx.site.host()))
    before_b_row = fleet_status(remote, ISOLATED_SITE)
    host = a.ctx.site.host()
    applied = judges.updater_state(remote.probe(host)).get("applied_gen")
    if judges.as_int(applied) < 1:
        # 읽기 실패를 gen 계산에 넣으면 같은 gen(no-op)·gen 0(PUT 422)이 되어 제품 실패로
        # 남는다(리뷰 L2)
        raise InfraError(f"cannot read applied gen of {SCHEDULED_SITE}: {applied!r}")
    gen = publish_gen(fleet_status(remote, SCHEDULED_SITE).get("max_published_gen"), applied)
    pair = session.pair
    # 창은 번들을 만들기 직전부터 잰다(번들 안 window.once에 들어가야 하므로). 번들은 초 단위로
    # 적으므로(`build_site_bundle`) 판정도 같은 초 단위 시각을 쓴다 — 마이크로초가 남으면 창이 열린
    # 직후 같은 초 안의 lock이 "창 전"으로 잘못 판정된다(리뷰 H1).
    start = (remote.now() + timedelta(seconds=WINDOW_LEAD_S)).replace(microsecond=0)
    began = remote.clock()
    pib = sc.build(a.ctx, gen, pair.next, (start, start + WINDOW_LEN), session.tree_of(pair.next))
    built = remote.clock()
    put_reply = put(remote, SCHEDULED_SITE, gen, pib)
    put_at = remote.now()
    # 어디가 느린지 바로 보게 빌드·PUT 시간을 따로 남긴다(늦은 PUT으로 멈춰도 로그·예외에 남는다)
    timings = {"build_s": round(built - began, 1), "put_s": round(remote.clock() - built, 1)}
    a.ctx.log(f"G2-2 bundle g{gen}: build {timings['build_s']}s, PUT {timings['put_s']}s")
    published = fleet_status(remote, SCHEDULED_SITE)
    if put_at > start - timedelta(seconds=PREFETCH_ALLOWANCE_S):
        raise InfraError(
            f"PUT g{gen} finished at {put_at.isoformat()}, too close to the window start "
            f"{start.isoformat()} to see verified < window (slow build host; "
            f"build {timings['build_s']}s, PUT {timings['put_s']}s)"
        )
    snap, waited = remote.converge(host, gen)
    row = fleet_session.wait_row(
        remote,
        SCHEDULED_SITE,
        lambda r: judges.sub(r, "current").get("gen") == gen,
        fleet_session.FLEET_TIMEOUT_S,
    )
    report = sc.report(host, pib)
    next_version = pair.releases[pair.next]
    g2_2 = judges.judge_published(put_reply, published, gen=gen, next_version=next_version)
    g2_2 |= judges.judge_scheduled(
        report,
        snap,
        row,
        gen=gen,
        window_start=start,
        next_digests=a.ctx.digests[pair.next],
        next_version=next_version,
    )
    after_b = judges.isolation_view(remote.probe(b.ctx.site.host()))
    after_b_row = fleet_status(remote, ISOLATED_SITE)
    g2_3 = judges.judge_isolation(before_b, after_b)
    g2_3 |= judges.judge_central_unchanged(before_b_row, after_b_row)
    a.ctx.record(
        ITEM_G2_2,
        all(g2_2.values()),
        gen=gen,
        checks=g2_2,
        bundle=pib.name,
        desired=published.get("desired"),
        window_start=start.isoformat(),
        put_at=put_at.isoformat(),
        **timings,
        timeline=report.get("timeline"),
        fetched_after_put_s=seconds_after(report, "fetched", put_at),
        verified_after_put_s=seconds_after(report, "verified", put_at),
        locked_after_window_s=seconds_after(report, "locked", start),
        clock_skew_s=restored.get("clock_skew_s"),
        converge_s=round(waited, 1),
        logs=None if all(g2_2.values()) else str(report.get("logs"))[-TAIL_CHARS:],
    )
    b.ctx.record(
        ITEM_G2_3,
        all(g2_3.values()),
        checks=g2_3,
        before=before_b,
        after=after_b,
        central=[
            {k: r.get(k) for k in ("desired", "max_published_gen")}
            for r in (before_b_row, after_b_row)
        ],
    )
    return ScheduledOutcome(g2_2, g2_3)
