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

M6-4b(나머지 시나리오는 창을 즉시 연다 — `publish`, 창 판정은 G2-2만):
- `revert`(G2-4 revert, site-a c1): FAULT-h 게시 → 자동 롤백 → BASE 다시 게시 → 적용·중앙 current.
- `resume`(G2-6 ① 내려받기 이어받기, site-a c1): VM nft로 중앙 응답을 느리게 → NEXT 게시 →
  `.part`가 `PART_MIN_BYTES`를 넘으면 전부 차단 → Updater 실패 로그·`.part` 유지 → 카운터만 두고
  해제 → 적용·해제 뒤 받은 바이트로 이어받기 판정. nft 표는 끝에 반드시 지운다.
- `writes`(G2-W, site-b c1): 스위치 꺼짐 거부 → `writes.enabled` 번들 게시 → 선언 태그 쓰기 →
  미선언 태그 거부(`judges.judge_writes`).
- `fleet_view`(G2-9, site-b c1): VM nft로 중앙 차단 → control-web 경로(`central.py fleet --ui`)로
  stale → 해제 → online.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from harness import buildhost, fleet_session, g2, golden, judges, site
from harness.context import TAIL_CHARS, Context, E2EError, InfraError
from harness.fleet_session import FleetSession, Remote
from harness.vm import REMOTE_ERRORS, Vm

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


class Build(Protocol):
    def __call__(
        self,
        ctx: Context,
        gen: int,
        release: str,
        window: tuple[datetime, datetime],
        tree: Path,
        *,
        writes: bool = False,
    ) -> Path: ...


def build_bundle(
    ctx: Context,
    gen: int,
    release: str,
    window: tuple[datetime, datetime],
    tree: Path,
    *,
    writes: bool = False,
) -> Path:
    """G2 현장 번들(nightly 작업 디렉터리의 기록·store, 트리 `tree`, `window.once` = `window`,
    `writes`면 현장 쓰기 스위치를 켠다)."""
    return buildhost.build_site_bundle(
        ctx,
        gen,
        release,
        window[0],
        golden.wsl_path(tree),
        work=buildhost.NIGHTLY_WORK,
        window_close_at=window[1],
        writes_enabled=writes,
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
    """ci 토큰 `PUT /bundles/{site}/{gen}`(`central.py put`). 201이 아니면 제품(`E2EError` — 중앙이
    서명 번들을 받지 않았다). WSL 실패는 `InfraError`이되, 그때 `/fleet`을 다시 읽어 중앙
    `max_published_gen`이 이미 gen 이상이면 제품이다(리뷰 L3): control-api는 gen ≤ 최신이면
    본문을 받기 전에 409로 연결을 닫고, `central.py`(urllib)는 0.7 GB를 보내다 SSL EOF로 죽어
    WSL 실패로 보인다 — 같은 gen 재게시(gen 계산 회귀)를 infra 재시도로 숨기지 않게."""
    try:
        done = fleet_session.central(
            remote, "put", "--site", site_id, "--gen", str(gen), "--pib", golden.wsl_path(pib)
        )
    except InfraError as exc:
        published = judges.as_int(fleet_status(remote, site_id).get("max_published_gen"))
        if published >= gen:
            raise E2EError(
                f"PUT /bundles/{site_id}/{gen} failed ({exc}) while /fleet max_published_gen is "
                f"{published} >= {gen}: presumably 409 before the body (same or older gen)"
            ) from exc
        raise
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


# ---------------------------------------------------------------- 공통(M6-4b)

IMMEDIATE = timedelta(minutes=5)
"""창을 즉시 연다: `window.once` = 번들을 만들기 직전 now - 5분 ~ + `WINDOW_LEN`(c0 시나리오
`scenarios.build_bundle`과 같은 5분). 창 판정은 G2-2만 하므로 내려받기 지연이 다른 판정에 섞이지
않는다(리뷰 L1)."""


@dataclass(frozen=True)
class Published:
    gen: int
    applied: int
    """게시 직전 현장 applied gen(롤백이면 그대로여야 한다)."""
    pib: Path
    put: dict[str, object]


def publish(
    sc: Scenario,
    session: g2.Session,
    run: g2.SiteRun,
    release: str,
    *,
    tree: Path | None = None,
    writes: bool = False,
) -> Published:
    """`run` 현장에 `release` 번들(트리 `tree`, 없으면 `session.tree_of(release)`, 창 즉시)을 직접
    PUT한다. gen = `publish_gen`(중앙 max, 현장 applied). applied를 못 읽으면 PUT 전에
    `InfraError`(리뷰 L2)."""
    remote = sc.remote
    site_id = run.ctx.site.site_id
    applied = judges.updater_state(remote.probe(run.ctx.site.host())).get("applied_gen")
    if judges.as_int(applied) < 1:
        raise InfraError(f"cannot read applied gen of {site_id}: {applied!r}")
    gen = publish_gen(fleet_status(remote, site_id).get("max_published_gen"), applied)
    start = (remote.now() - IMMEDIATE).replace(microsecond=0)
    window = (start, start + WINDOW_LEN)
    pib = sc.build(run.ctx, gen, release, window, tree or session.tree_of(release), writes=writes)
    run.ctx.log(f"publish g{gen} {release}" + (" (writes enabled)" if writes else ""))
    return Published(gen, judges.as_int(applied), pib, put(remote, site_id, gen, pib))


def current_row(
    remote: Remote, site_id: str, gen: int, snap: dict[str, object]
) -> dict[str, object]:
    """적용을 마친 현장(applied == gen)이면 중앙 `/fleet` current가 gen이 될 때까지 기다린 행,
    아니면 지금 행 — 제품 실패(적용 안 됨)를 대기 시간 초과(infra)로 바꾸지 않게."""
    if judges.updater_state(snap).get("applied_gen") != gen:
        return fleet_status(remote, site_id)
    return fleet_session.wait_row(
        remote,
        site_id,
        lambda r: judges.sub(r, "current").get("gen") == gen,
        fleet_session.FLEET_TIMEOUT_S,
    )


def logs_tail(report: dict[str, object]) -> str:
    return str(report.get("logs"))[-TAIL_CHARS:]


# ---------------------------------------------------------------- G2-4 revert (site-a)

REVERT_SITE = "site-a"
G2_4_REVERT_ITEMS = (
    "fault_put_201",
    *("rollback_result", "rollback_code", "rollback_no_manual_alarm", "rollback_applied_gen"),
    *("rollback_images_match", "rollback_edit_unlocked"),
    "revert_put_201",
    *("revert_result", "revert_no_manual_alarm", "revert_applied_gen", "revert_images_match"),
    *("revert_edit_unlocked", "fleet_current"),
)
"""G2-4 revert 판정 항목(시험 `test_g2_4_revert[…]`) — `judges.judge_revert`."""
ITEM_G2_4_REVERT = "G2-4 revert via the central (FAULT-h -> rollback -> BASE re-published)"


def revert(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """G2-4 revert(모듈 설명). c1에서 site-a는 BASE·applied g1이다: FAULT-h g2 → 롤백 → BASE g3."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    sc.restore(session, fs, (REVERT_SITE,), remote=remote)
    run = g2.site_up(session, REVERT_SITE)
    host = run.ctx.site.host()
    pair = session.pair
    fault = publish(sc, session, run, pair.fault)
    fault_snap, fault_s = remote.converge(host, fault.gen)
    fault_report = sc.report(host, fault.pib)
    again = publish(sc, session, run, run.release)
    snap, waited = remote.converge(host, again.gen)
    report = sc.report(host, again.pib)
    row = current_row(remote, REVERT_SITE, again.gen, snap)
    checks = judges.judge_revert(
        (fault.put, fault_snap, fault_report),
        (again.put, snap, report),
        row,
        c1_applied=fault.applied,
        revert_gen=again.gen,
        base_digests=run.ctx.digests[run.release],
        base_version=pair.releases[run.release],
    )
    ok = all(checks.values())
    run.ctx.record(
        ITEM_G2_4_REVERT,
        ok,
        checks=checks,
        fault_gen=fault.gen,
        revert_gen=again.gen,
        fault_code=fault_report.get("code"),
        rolling_back_s=judges.rolling_back_seconds(fault_report),
        fault_converge_s=round(fault_s, 1),
        revert_converge_s=round(waited, 1),
        current=row.get("current"),
        logs=None if ok else [logs_tail(fault_report), logs_tail(report)],
    )
    return checks


# ---------------------------------------------------------------- 중앙 응답 필터(VM nft)

CLEAR_FILTER = f"table inet {site.FLEET_NFT}\ndelete table inet {site.FLEET_NFT}\n"
"""필터 표를 지운다(없어도 된다 — 먼저 만들고 지우는 한 트랜잭션)."""


def central_filter(drop: str | None) -> str:
    """VM `e2e_fleet` 표를 통째로 바꾸는 nft 스크립트: 중앙(10.77.0.1:28443) → VM 수신을 세는
    카운터(`central_in` — 표를 다시 만들 때마다 0) 뒤에 `drop`(None = 세기만, `"drop"` = 전부 차단,
    `"limit rate over … drop"` = 속도 제한). input 훅이라 ssh(호스트 → VM:22)는 그대로다."""
    match = f"ip saddr {site.CENTRAL_HOST} tcp sport {site.CENTRAL_PORT}"
    rules = [f'{match} counter comment "{site.CENTRAL_IN}"']
    if drop is not None:
        rules.append(f"{match} {drop}")
    return CLEAR_FILTER + "\n".join(
        [
            f"table inet {site.FLEET_NFT} {{",
            "    chain central_in {",
            "        type filter hook input priority -5; policy accept;",
            *(f"        {rule}" for rule in rules),
            "    }",
            "}",
            "",
        ]
    )


def clear_filter(remote: Remote, host: Vm, ctx: Context) -> bool:
    """필터 표를 지운다. 실패는 로그만 남기고 거짓 — 이미 다른 예외가 올라가는 중일 수 있고, 다음
    c1·c0 복원이 VM 커널 상태(nft 포함)를 되돌린다."""
    try:
        remote.nft(host, CLEAR_FILTER)
    except (*REMOTE_ERRORS, subprocess.TimeoutExpired) as exc:
        ctx.log(f"nft table {site.FLEET_NFT} not cleared: {exc}")
        return False
    return True


# ---------------------------------------------------------------- G2-6 ① 이어받기 (site-a)

RESUME_SITE = "site-a"
RESUME_LIMIT = "limit rate over 4096 kbytes/second drop"
"""속도 제한 4 MiB/s(nft가 넘는 패킷을 버린다). `PART_MIN_BYTES`까지 이상적으로 32초. 버려서 누르는
TCP는 이보다 느리고 들쭉날쭉하다(계획 K1 — 실측 1회로 맞춘다). 남은 번들(약 0.6 GB)을 이 속도로
다 받기까지 약 2.5분이라 기준을 넘긴 뒤 차단할 시간이 넉넉하다."""
PART_MIN_BYTES = 128 * 2**20
"""차단 전 `.part` 기준(128 MiB). 이어받기 판정(해제 뒤 받은 바이트 < 번들 - `.part`/2)의 여유가
`.part`/2 = 64 MiB다. nft 카운터는 IP 패킷 길이를 세므로 헤더·TLS 몫(최악 약 4% — 1500바이트
패킷마다 52바이트, 남은 0.6 GB면 약 25 MB)이 얹힌다. 32 MiB 기준이면 이 몫에 여유가 먹힌다."""
PART_WAIT_S = 240.0
"""`.part`가 기준을 넘기까지 상한 = 이상적 32초 + poll 10초의 약 5배(버려서 누르는 TCP). 넘으면
`InfraError`(속도 제한 환경 탓)."""
PART_POLL_S = 2.0
BLOCK_WAIT_S = 120.0
"""전부 차단한 뒤 Updater 실패 로그(`poll failed`)를 기다리는 상한 = 읽기 시한 30초(E1) + poll 10초
+ 다음 연결 시한 30초에 여유. 넘어도 멈추지 않고 판정(`interrupted_logged` 거짓)으로 둔다."""
FAILURE_POLL_S = 5.0
G2_6_1_ITEMS = (
    *("put_201", "interrupted_logged", "part_kept", "resumed"),
    *("succeeded", "images_next", "within_10min"),
)
"""G2-6 ① 판정 항목(시험 `test_g2_6_1_resume[…]`) — `judges.judge_resume`."""
ITEM_G2_6_1 = "G2-6 1 interrupted download resumes (Range)"


def wait_part(remote: Remote, host: Vm) -> tuple[int, float]:
    """`.part`가 `PART_MIN_BYTES`를 넘을 때까지(크기, 걸린 초). 상한을 넘거나 `.part`가 기준 전에
    사라지면(속도 제한 아래서 다 받았다) `InfraError`."""
    started = remote.clock()
    seen: int | None = None
    while True:
        part = remote.part_bytes(host)
        took = remote.clock() - started
        if part is not None and part > PART_MIN_BYTES:
            return part, round(took, 1)
        if part is None and seen is not None:
            raise InfraError(
                f".part vanished at {seen} bytes before the block (download finished under "
                f"'{RESUME_LIMIT}'?)"
            )
        seen = part if part is not None else seen
        if took >= PART_WAIT_S:
            raise InfraError(
                f".part did not pass {PART_MIN_BYTES} bytes within {PART_WAIT_S:.0f}s under "
                f"'{RESUME_LIMIT}' (last {seen})"
            )
        remote.sleep(PART_POLL_S)


def wait_failures(remote: Remote, host: Vm, since: float) -> int:
    """차단 뒤 Updater `poll failed` 줄이 생길 때까지(상한 `BLOCK_WAIT_S`) — 마지막 줄 수."""
    started = remote.clock()
    while True:
        count = remote.poll_failures(host, since)
        if count >= 1 or remote.clock() - started >= BLOCK_WAIT_S:
            return count
        remote.sleep(FAILURE_POLL_S)


def resume(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """G2-6 ①(모듈 설명). 필터는 PUT 전에 건다(첫 바이트부터 느리게)."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    sc.restore(session, fs, (RESUME_SITE,), remote=remote)
    run = g2.site_up(session, RESUME_SITE)
    host = run.ctx.site.host()
    pair = session.pair
    remote.nft(host, central_filter(RESUME_LIMIT))
    try:
        pub = publish(sc, session, run, pair.next)
        desired = judges.sub(fleet_status(remote, RESUME_SITE), "desired")
        part_at_block, part_s = wait_part(remote, host)
        blocked_at = remote.vm_epoch(host)
        remote.nft(host, central_filter("drop"))
        failures = wait_failures(remote, host, blocked_at)
        part_at_release = remote.part_bytes(host)
        remote.nft(host, central_filter(None))  # 해제 — 카운터만 0부터 다시 센다
        snap, waited = remote.converge(host, pub.gen)
        received = remote.central_in_bytes(host)
    finally:
        clear_filter(remote, host, run.ctx)
    report = sc.report(host, pub.pib)
    size = judges.as_int(desired.get("size"))
    checks = judges.judge_resume(
        pub.put,
        snap,
        gen=pub.gen,
        next_digests=run.ctx.digests[pair.next],
        part_at_block=part_at_block,
        part_at_release=part_at_release,
        poll_failures=failures,
        received=received,
        size=size,
        converge_s=waited,
    )
    ok = all(checks.values())
    run.ctx.record(
        ITEM_G2_6_1,
        ok,
        checks=checks,
        gen=pub.gen,
        size=size,
        part_at_block=part_at_block,
        part_wait_s=part_s,
        part_at_release=part_at_release,
        poll_failures=failures,
        received_after_release=received,
        converge_s=round(waited, 1),
        logs=None if ok else logs_tail(report),
    )
    return checks


# ---------------------------------------------------------------- G2-W 현장 쓰기 (site-b)

WRITE_SITE = "site-b"
WRITE_TAG = "line1/temp_sp"
"""fixture `acme`가 쓰기를 선언한 setpoint(holding 100 float32 — 시뮬레이터 쓰기 메모리 칸(E3),
범위 20~90, 선언 role 기본 operator). G2 트리 = c1 트리에 이미 있다(계획 E4 정정, 코디네이터
확정) — G2-W 번들은 c1과 같은 트리에 `writes.enabled`만 다르다."""
UNDECLARED_TAG = "sim/r001"
"""쓰기 선언이 없는 G2 태그(holding 1 — 시뮬레이터 사인파 칸)."""
OFF_VALUE = 77.5
ON_VALUES = (42.5, 57.5)
"""켠 뒤 쓸 값 — c1 값(꺼짐 때 읽은 값)과 다른 첫 값. 범위 안이고 float32로 정확해 readback을
그대로 비교한다."""
UNDECLARED_VALUE = 7.0
"""사인파 칸(500~1500)이 내지 않는 값."""
TAG_GOOD_TIMEOUT_S = 180.0
"""켠 번들 적용(core 재시작) 뒤 선언 태그가 GOOD이 되기까지(K2 — collector가 새 plan으로 보고해야
쓴다). 넘어도 멈추지 않는다 — 그 뒤 쓰기가 `tag_good`·`plan_reported`로 거부되어 판정이 거짓이다."""
VALUE_TIMEOUT_S = 30.0
"""쓴 값이 `GET /tags`에 보이기까지(장비 poll 1초 + live 읽기 간격 10초)."""
G2_W_ITEMS = (
    *("off_rejected", "off_audited", "off_value_unchanged"),
    *("enable_put_201", "enable_succeeded"),
    *("on_confirmed", "on_readback", "on_audited", "on_tag_value"),
    *("undeclared_rejected", "undeclared_audited", "undeclared_not_written"),
)
"""G2-W 판정 항목(시험 `test_g2_w_writes[…]`) — `judges.judge_writes`."""
ITEM_G2_W = "G2-W site writes (switch off -> writes.enabled bundle -> declared -> undeclared)"


def writes(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """G2-W(모듈 설명). 계정은 engineer 하나(선언 role operator 이상 + `GET /audit` engineer
    이상)."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    sc.restore(session, fs, (WRITE_SITE,), remote=remote)
    run = g2.site_up(session, WRITE_SITE)
    host = run.ctx.site.host()
    account = remote.add_user(host, site.E2E_ENGINEER, "engineer", site.ENGINEER_PASSWORD)
    reason = f"e2e G2-W {session.run_id}"

    def value_of(tag: str) -> object:
        return judges.tag_value(remote.live(host), tag)

    before = value_of(WRITE_TAG)
    off = remote.write_tag(host, WRITE_TAG, OFF_VALUE, f"{reason} off")
    after = value_of(WRITE_TAG)
    # c1(g1)과 같은 G2 트리에 스위치만 켠다 — NEXT 트리(태그 설명 하나가 다름)가 아니다
    pub = publish(sc, session, run, session.pair.next, tree=session.tree, writes=True)
    snap, waited = remote.converge(host, pub.gen)
    _, good, good_s = site.wait_live(
        lambda: remote.live(host),
        lambda out: {"good": judges.tag_value(out, WRITE_TAG) is not None},
        TAG_GOOD_TIMEOUT_S,
        clock=remote.clock,
        sleep=remote.sleep,
    )
    value = next(v for v in ON_VALUES if v != before)
    on = remote.write_tag(host, WRITE_TAG, value, f"{reason} on")
    seen, _, seen_s = site.wait_live(
        lambda: remote.live(host),
        lambda out: {"value": judges.tag_value(out, WRITE_TAG) == value},
        VALUE_TIMEOUT_S,
        clock=remote.clock,
        sleep=remote.sleep,
    )
    undeclared = remote.write_tag(host, UNDECLARED_TAG, UNDECLARED_VALUE, f"{reason} undeclared")
    checks = judges.judge_writes(
        off,
        (pub.put, snap),
        on,
        undeclared,
        tags=(WRITE_TAG, UNDECLARED_TAG),
        values=(OFF_VALUE, value, UNDECLARED_VALUE),
        seen=(before, after, judges.tag_value(seen, WRITE_TAG), value_of(UNDECLARED_TAG)),
        gen=pub.gen,
    )
    ok = all(checks.values())
    run.ctx.record(
        ITEM_G2_W,
        ok,
        checks=checks,
        gen=pub.gen,
        account={k: account.get(k) for k in ("rc", "stderr")},
        values={"before": before, "after_off": after, "written": value},
        tag_good_s=good_s if all(good.values()) else None,
        value_seen_s=seen_s,
        converge_s=round(waited, 1),
        replies=None if ok else {"off": off, "on": on, "undeclared": undeclared},
    )
    return checks


# ---------------------------------------------------------------- G2-9 중앙 화면 stale (site-b)

VIEW_SITE = "site-b"
STALE_LIMIT_S = 120.0
"""차단 → stale 상한. 중앙 stale = 마지막 수신 > 3 x poll 10초 = 30초(E7) + 막히기 직전 heartbeat
시점·읽기 간격의 여유(계획 R4)."""
ONLINE_LIMIT_S = 60.0
"""해제 → online 상한 = 막혀 있던 연결의 시한(heartbeat 10초·poll 30초) + 다음 poll 10초에 여유."""
VIEW_POLL_S = 5.0
G2_9_ITEMS = ("via_control_web", "online_before", "stale_after_block", "online_after_release")
"""G2-9 판정 항목(시험 `test_g2_9_fleet_view[…]`) — `judges.judge_fleet_view`."""
ITEM_G2_9 = "G2-9 central fleet view: stale while cut off, online again (control-web path)"


def ui_fleet(remote: Remote) -> dict[str, object]:
    """control-web과 같은 경로(Caddy basic auth → `/ui/fleet`, `central.py fleet --ui`)."""
    return fleet_session.central(remote, "fleet", "--ui")


def wait_connection(remote: Remote, site_id: str, want: str, limit_s: float) -> float | None:
    """control-web 경로로 읽은 `site_id` 행의 `connection`이 `want`가 될 때까지(걸린 초). 상한까지
    바뀌지 않으면 None(판정 거짓 — 중앙이 바꾸지 않은 것은 제품이다)."""
    started = remote.clock()
    while True:
        row = judges.fleet_row(ui_fleet(remote), site_id)
        took = remote.clock() - started
        if row.get("connection") == want:
            return round(took, 1)
        if took >= limit_s:
            return None
        remote.sleep(VIEW_POLL_S)


def fleet_view(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """G2-9(모듈 설명). 차단은 반드시 푼다 — 풀지 못하면 `InfraError`."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    sc.restore(session, fs, (VIEW_SITE,), remote=remote)
    run = g2.site_up(session, VIEW_SITE)
    host = run.ctx.site.host()
    first = ui_fleet(remote)
    remote.nft(host, central_filter("drop"))
    cleared = False
    try:
        stale_s = wait_connection(remote, VIEW_SITE, "stale", STALE_LIMIT_S)
    finally:
        cleared = clear_filter(remote, host, run.ctx)
    if not cleared:
        raise InfraError(f"could not lift the central block on {VIEW_SITE}")
    online_s = wait_connection(remote, VIEW_SITE, "online", ONLINE_LIMIT_S)
    checks = judges.judge_fleet_view(
        first,
        VIEW_SITE,
        stale_s,
        online_s,
        stale_limit_s=STALE_LIMIT_S,
        online_limit_s=ONLINE_LIMIT_S,
    )
    run.ctx.record(
        ITEM_G2_9,
        all(checks.values()),
        checks=checks,
        via=first.get("via"),
        block_to_stale_s=stale_s,
        release_to_online_s=online_s,
    )
    return checks
