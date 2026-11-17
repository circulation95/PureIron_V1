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

M6-4b-2:
- `airgap`(G2-7, site-b **c0** + 중앙 c1 DB): seq 정렬 → VM nft로 10.77.0.1 쪽 ssh 외 전부 차단·
  카운터 → NEXT gen+1 번들을 USB 대용 디스크로 → `pureiron-site apply`(현장 USB 절차) → 같은
  디스크를 rw로 다시 마운트해 `report export` → 떼고 WSL에서 꺼냄 → 호스트
  `POST /offline-imports`(admin) → `/fleet`·control-web 경로 반영. 중앙 PUT 없음. nft 표·USB는
  끝에 반드시 치운다.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from harness import buildhost, fleet_session, g2, golden, judges, site, usb
from harness.context import TAIL_CHARS, Context, E2EError, InfraError
from harness.fleet_session import FleetSession, Remote
from harness.vm import REMOTE_ERRORS, Vm, VmError

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
    restore_airgap: Callable[..., dict[str, object]] = fleet_session.restore_airgap
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


def bundle_sha256(pib: Path) -> str:
    with pib.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def put(remote: Remote, site_id: str, gen: int, pib: Path) -> dict[str, object]:
    """ci 토큰 `PUT /bundles/{site}/{gen}`(`central.py put`). 201이 아니면 제품(`E2EError` — 중앙이
    서명 번들을 받지 않았다). WSL 실패는 `InfraError`이되, 그때 `/fleet`을 다시 읽어 중앙
    `max_published_gen`이 이미 gen 이상이면 제품이다(리뷰 L3): control-api는 gen ≤ 최신이면
    본문을 받기 전에 409로 연결을 닫고, `central.py`(urllib)는 0.7 GB를 보내다 SSL EOF로 죽어
    WSL 실패로 보인다 — 같은 gen 재게시(gen 계산 회귀)를 infra 재시도로 숨기지 않게. 다만 다시
    읽은 desired가 이 gen·이 번들 sha256이면 중앙이 받은 뒤 응답만 잃은 것이라 성공으로 보고
    진행한다(fix r1 L4)."""
    try:
        done = fleet_session.central(
            remote, "put", "--site", site_id, "--gen", str(gen), "--pib", golden.wsl_path(pib)
        )
    except InfraError as exc:
        try:
            row = fleet_status(remote, site_id)
        except InfraError as again:
            raise InfraError(
                f"PUT /bundles/{site_id}/{gen} failed ({exc}); /fleet re-read failed too ({again})"
            ) from exc
        desired = judges.sub(row, "desired")
        sha = str(desired.get("sha256") or "").removeprefix("sha256:")
        if desired.get("generation") == gen and sha == bundle_sha256(pib):
            return {"status": 201, "body": None, "reply_lost": str(exc)[-TAIL_CHARS:]}
        published = judges.as_int(row.get("max_published_gen"))
        if published >= gen:
            raise E2EError(
                f"PUT /bundles/{site_id}/{gen} failed ({exc}) while /fleet max_published_gen is "
                f"{published} >= {gen}: presumably 409 before the body (same or older gen)"
            ) from exc
        raise
    if done.get("status") != 201:
        raise E2EError(f"PUT /bundles/{site_id}/{gen} -> {done.get('status')}: {done.get('body')}")
    return done


def logs_tail(report: dict[str, object]) -> str:
    return str(report.get("logs"))[-TAIL_CHARS:]


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
    pair = session.pair
    # 창은 번들을 만들기 직전부터 잰다(번들 안 window.once에 들어가야 하므로 — `publish`)
    pub = publish(sc, session, a, pair.next, window_lead_s=WINDOW_LEAD_S)
    gen, pib, put_reply, start = pub.gen, pub.pib, pub.put, pub.window[0]
    put_at = remote.now()
    # 어디가 느린지 바로 보게 빌드·PUT 시간을 따로 남긴다(늦은 PUT으로 멈춰도 로그·예외에 남는다)
    timings = {"build_s": pub.build_s, "put_s": pub.put_s}
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
        logs=None if all(g2_2.values()) else logs_tail(report),
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
    window: tuple[datetime, datetime]
    build_s: float
    put_s: float


def publish(
    sc: Scenario,
    session: g2.Session,
    run: g2.SiteRun,
    release: str,
    *,
    tree: Path | None = None,
    writes: bool = False,
    window_lead_s: float | None = None,
) -> Published:
    """`run` 현장에 `release` 번들(트리 `tree`, 없으면 `session.tree_of(release)`)을 직접 PUT한다.
    gen = `publish_gen`(중앙 max, 현장 applied). applied를 못 읽으면 PUT 전에 `InfraError`(리뷰
    L2). 창은 번들을 만들기 직전에 정한다: `window_lead_s`가 없으면 즉시(`IMMEDIATE`), 있으면 now
    + 그 초(G2-2). 번들은 초 단위로 적으므로 창 시작도 같은 초 단위다 — 마이크로초가 남으면 창이
    열린 직후 같은 초 안의 lock이 "창 전"으로 잘못 판정된다(리뷰 H1)."""
    remote = sc.remote
    site_id = run.ctx.site.site_id
    applied = judges.updater_state(remote.probe(run.ctx.site.host())).get("applied_gen")
    if judges.as_int(applied) < 1:
        # 읽기 실패를 gen 계산에 넣으면 같은 gen(no-op)·gen 0(PUT 422)이 되어 제품 실패로
        # 남는다(리뷰 L2)
        raise InfraError(f"cannot read applied gen of {site_id}: {applied!r}")
    gen = publish_gen(fleet_status(remote, site_id).get("max_published_gen"), applied)
    lead = -IMMEDIATE if window_lead_s is None else timedelta(seconds=window_lead_s)
    start = (remote.now() + lead).replace(microsecond=0)
    window = (start, start + WINDOW_LEN)
    began = remote.clock()
    pib = sc.build(run.ctx, gen, release, window, tree or session.tree_of(release), writes=writes)
    built = remote.clock()
    reply = put(remote, site_id, gen, pib)
    put_s = round(remote.clock() - built, 1)
    run.ctx.log(f"publish g{gen} {release}" + (" (writes enabled)" if writes else ""))
    return Published(
        gen, judges.as_int(applied), pib, reply, window, round(built - began, 1), put_s
    )


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


def clear_table(name: str) -> str:
    """nft 표 `inet <name>`을 지운다(없어도 된다 — 먼저 만들고 지우는 한 트랜잭션)."""
    return f"table inet {name}\ndelete table inet {name}\n"


CLEAR_FILTER = clear_table(site.FLEET_NFT)


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


def clear_filter(remote: Remote, host: Vm, ctx: Context, script: str = CLEAR_FILTER) -> bool:
    """필터 표를 지운다(`script` — 기본 `e2e_fleet`). 실패는 로그만 남기고 거짓 — 이미 다른 예외가
    올라가는 중일 수 있고, 다음 c1·c0 복원이 VM 커널 상태(nft 포함)를 되돌린다."""
    try:
        remote.nft(host, script)
    except (*REMOTE_ERRORS, subprocess.TimeoutExpired) as exc:
        ctx.log(f"nft table not cleared ({script.splitlines()[0]}): {exc}")
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
        try:
            part = remote.part_bytes(host)
        except VmError:  # ssh 한 번 끊김 — 상한까지 다시 묻는다(fix r1 L1)
            part = seen
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


def wait_failures(remote: Remote, host: Vm, since: float) -> tuple[int, float | None]:
    """차단 뒤 Updater `poll failed` 줄이 생길 때까지(상한 `BLOCK_WAIT_S`) — (마지막 줄 수, 처음
    보인 때까지 초 — 못 보면 None). 초는 읽기 시한 30초 가정(E1)을 실측으로 맞출 값이다(fix r1
    L9). ssh가 한 번 끊기면 다시 묻는다(L1)."""
    started = remote.clock()
    while True:
        try:
            count = remote.poll_failures(host, since)
        except VmError:
            count = 0
        took = remote.clock() - started
        if count >= 1:
            return count, round(took, 1)
        if took >= BLOCK_WAIT_S:
            return count, None
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
        failures, failure_s = wait_failures(remote, host, blocked_at)
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
        block_to_failure_s=failure_s,
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
TAG_GOOD_TIMEOUT_S = 180.0
"""선언 태그가 GOOD이 되기까지 — c1 복원 뒤(꺼짐 쓰기 전)와 켠 번들 적용(core 재시작) 뒤 둘 다(K2
— collector가 새 plan으로 보고해야 쓴다). 넘어도 멈추지 않는다 — 그 뒤 쓰기가 일시 검사로 거부되면
`write_settled`가 다시 묻고, 끝내 거부되면 판정이 거짓이다."""
TRANSIENT_CHECKS = frozenset(
    {"tag_good", "plan_reported", "single_collector_session", "device_connected"}
)
"""쓰기 직전 잠깐 거짓일 수 있는 검사(fix r1 M1): core 재시작·메모리 복원 직후 collector 세션이
다시 붙고, replay batch는 값을 GOOD으로 만들지만 `reported_plan_rev`는 갱신하지 않는다(core
`acquisition/server.py`) — 태그 GOOD만 기다려서는 `plan_reported`가 아직 거짓일 수 있다."""
EXPECTED_REJECTS = frozenset({"writes_enabled", "declared"})
"""시나리오가 일부러 내는 거부 — 다시 묻는 대상이 아니다."""
WRITE_RETRY_S = 60.0
"""일시 검사로 거부된 쓰기를 다시 묻는 상한 — 장비 poll 1초·collector 재접속·첫 live batch에
충분하다. 넘으면 마지막 응답으로 판정한다(거짓 = product)."""
WRITE_RETRY_POLL_S = 5.0
VALUE_TIMEOUT_S = 30.0
"""쓴 값이 `GET /tags`에 보이기까지(장비 poll 1초 + live 읽기 간격 10초)."""
G2_W_ITEMS = (
    *("off_rejected", "off_audited", "off_value_unchanged"),
    *("enable_put_201", "enable_succeeded"),
    *("on_confirmed", "on_readback", "on_audited", "on_tag_value"),
    *("undeclared_rejected", "undeclared_audited"),
)
"""G2-W 판정 항목(시험 `test_g2_w_writes[…]`) — `judges.judge_writes`."""
ITEM_G2_W = "G2-W site writes (switch off -> writes.enabled bundle -> declared -> undeclared)"


def transient_reject(out: dict[str, object]) -> bool:
    """`site_api.py write` 응답이 일시 검사로만 거부됐는가: 409이고(confirm이 있으면 confirm,
    없으면 prepare) 실패 검사에서 시나리오가 일부러 내는 거부(`EXPECTED_REJECTS`)를 뺀 나머지가
    비어 있지 않고 모두 `TRANSIENT_CHECKS`다."""
    status, body = judges.reply(out, "confirm" if out.get("confirm") else "prepare")
    rest = set(judges.failed_checks(body)) - EXPECTED_REJECTS
    return status == 409 and bool(rest) and rest <= TRANSIENT_CHECKS


def write_settled(
    remote: Remote, host: Vm, tag: str, value: float, reason: str
) -> tuple[dict[str, object], int]:
    """`write_tag`를 부르고, 일시 검사로만 거부되면(`transient_reject`) `WRITE_RETRY_POLL_S`마다
    다시 부른다(상한 `WRITE_RETRY_S`). (마지막 응답, 시도 횟수). 같은 사유로 부르므로 마지막
    응답의 감사 행에는 앞 시도의 거부 행도 들어 있다 — 판정은 결과 행을 write_id로, 거부 행을 실패
    검사 목록으로 고르므로 그대로 맞다."""
    started = remote.clock()
    tries = 0
    while True:
        out = remote.write_tag(host, tag, value, reason)
        tries += 1
        if not transient_reject(out) or remote.clock() - started >= WRITE_RETRY_S:
            return out, tries
        remote.sleep(WRITE_RETRY_POLL_S)


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

    def wait_good() -> float | None:
        _, good, took = site.wait_live(
            lambda: remote.live(host),
            lambda out: {"good": judges.tag_value(out, WRITE_TAG) is not None},
            TAG_GOOD_TIMEOUT_S,
            clock=remote.clock,
            sleep=remote.sleep,
        )
        return took if all(good.values()) else None

    off_good_s = wait_good()  # c1 메모리 복원 직후 세션·품질이 다시 잡히기까지(fix r1 M1)
    before = value_of(WRITE_TAG)
    off, off_tries = write_settled(remote, host, WRITE_TAG, OFF_VALUE, f"{reason} off")
    after = value_of(WRITE_TAG)
    # c1(g1)과 같은 G2 트리에 스위치만 켠다 — NEXT 트리(태그 설명 하나가 다름)가 아니다
    pub = publish(sc, session, run, session.pair.next, tree=session.tree, writes=True)
    snap, waited = remote.converge(host, pub.gen)
    good_s = wait_good()
    value = next(v for v in ON_VALUES if v != before)
    on, on_tries = write_settled(remote, host, WRITE_TAG, value, f"{reason} on")
    seen, _, seen_s = site.wait_live(
        lambda: remote.live(host),
        lambda out: {"value": judges.tag_value(out, WRITE_TAG) == value},
        VALUE_TIMEOUT_S,
        clock=remote.clock,
        sleep=remote.sleep,
    )
    undeclared, undeclared_tries = write_settled(
        remote, host, UNDECLARED_TAG, UNDECLARED_VALUE, f"{reason} undeclared"
    )
    checks = judges.judge_writes(
        off,
        (pub.put, snap),
        on,
        undeclared,
        tags=(WRITE_TAG, UNDECLARED_TAG),
        on_value=value,
        seen=(before, after, judges.tag_value(seen, WRITE_TAG)),
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
        tries={"off": off_tries, "on": on_tries, "undeclared": undeclared_tries},
        off_tag_good_s=off_good_s,
        tag_good_s=good_s,
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
    바뀌지 않거나 끝내 읽지 못하면(401·502·WSL 실패 = 빈 행) None — 판정 거짓."""
    started = remote.clock()
    while True:
        try:
            row = judges.fleet_row(ui_fleet(remote), site_id)
        except InfraError:  # WSL 한 번 흔들림 — 빈 행으로 치고 상한까지 다시 묻는다(fix r1 L2)
            row = {}
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


# ---------------------------------------------------------------- G2-7 폐쇄망 (site-b c0)

AIRGAP_SITE = "site-b"
AIRGAP_NFT = "e2e_airgap"
"""G2-7이 잠시 두는 nft 표 — `e2e_fleet`(G2-6 ①·G2-9)·외부 접속 카운터(`e2e_audit`)와 따로다."""
AIRGAP_COUNTERS = ("airgap_in", "airgap_out", "airgap_fwd_in", "airgap_fwd_out")
"""중앙(10.77.0.1)으로 가는/오는 패킷 중 ssh(VM:22) 외를 세고 버리는 규칙 comment — 모두 0이어야
한다(계획 F9: 외부 접속 카운터는 내부 망을 세지 않아 따로 둔다)."""
AIRGAP_LOG = "e2e-airgap-"
"""drop 규칙의 nft `log prefix` 머리(`e2e-airgap-in ` 등 — `e2e_audit`와 같은 방식). 카운터가 0이
아니면 VM 커널 로그의 이 줄을 기록에 남겨 출처(주소·포트)를 가린다(collect는 커널 로그를 모으지
않는다)."""
AIRGAP_LOG_LINES = 20
AIRGAP_CLEAR = clear_table(AIRGAP_NFT)
SSH_PORT = 22
REPORTS_DIR = "reports"
EXPORTED_MARK = "report(s) →"
"""`pureiron-site report export`가 묶음 파일마다 찍는 줄의 표지(`<n> report(s) → <경로>`) — 꺼낸
파일 수와 대조한다. 대기 보고가 없으면 `no pending reports`(0개 — 판정이 거짓, product)."""
"""USB 디스크 안 보고 묶음 디렉터리(`report export --out <마운트>/reports`)."""
G2_7_ITEMS = (
    *("no_central_traffic", "succeeded", "applied_gen", "images_next"),
    *("import_201", "import_applied", "fleet_last_attempt", "fleet_current"),
    *("via_control_web", "control_web_last_attempt"),
)
"""G2-7 판정 항목(시험 `test_g2_7_airgap[…]`) — `judges.judge_airgap`."""
ITEM_G2_7 = "G2-7 airgap (USB update -> report export -> offline import -> fleet)"


def airgap_filter() -> str:
    """VM `e2e_airgap` 표를 통째로 바꾸는 nft 스크립트. 체인마다 ssh(호스트 → VM:22와 그 응답)를
    먼저 통과시키고(accept는 이 체인만 끝낸다) 나머지 10.77.0.1 쪽은 세고·로그를 남기고 버린다.
    forward는 컨테이너(core·collector)가 중앙으로 가는 길이다. input은 unicast만 센다(`meta pkttype
    host`): 10.77.0.1은 VM의 게이트웨이·DNS(golden NETWORK_CONFIG)라 호스트 vNIC가 보내는 broadcast
    (10.77.0.255·255.255.255.255 — NetBIOS·앱 LAN 탐색)가 LOCAL_IN에 오고, 그것은 현장이 중앙에
    닿은 것이 아니다(M6-4b-2 리뷰 H1). 현장 → 중앙은 output·forward에서 먼저 잡힌다."""
    host = site.CENTRAL_HOST

    def drop(name: str, match: str) -> str:
        tag = name.removeprefix("airgap_").replace("_", "-")
        return f'{match} counter log prefix "{AIRGAP_LOG}{tag} " drop comment "{name}"'

    def chain(name: str, hook: str, rules: list[str]) -> list[str]:
        return [
            f"    chain {name} {{",
            f"        type filter hook {hook} priority -5; policy accept;",
            *(f"        {rule}" for rule in rules),
            "    }",
        ]

    return AIRGAP_CLEAR + "\n".join(
        [
            f"table inet {AIRGAP_NFT} {{",
            *chain(
                "airgap_in",
                "input",
                [
                    f"ip saddr {host} tcp dport {SSH_PORT} accept",
                    drop("airgap_in", f"ip saddr {host} meta pkttype host"),
                ],
            ),
            *chain(
                "airgap_out",
                "output",
                [
                    f"ip daddr {host} tcp sport {SSH_PORT} accept",
                    drop("airgap_out", f"ip daddr {host}"),
                ],
            ),
            *chain(
                "airgap_fwd",
                "forward",
                [
                    drop("airgap_fwd_in", f"ip saddr {host}"),
                    drop("airgap_fwd_out", f"ip daddr {host}"),
                ],
            ),
            "}",
            "",
        ]
    )


def airgap_log(sh: site.Shell) -> list[str]:
    """VM 커널 로그(nft `log`)의 `e2e-airgap-` 줄 끝 `AIRGAP_LOG_LINES`개 — 카운터가 0이 아닐 때의
    출처 증거. 못 읽으면 오류 한 줄."""
    command = (
        f"sudo journalctl -k -b --no-pager -o short-iso | grep -F '{AIRGAP_LOG}' "
        f"| tail -n {AIRGAP_LOG_LINES}"
    )
    try:
        out = sh.run(command, check=False, timeout_s=60).stdout
    except (*REMOTE_ERRORS, subprocess.TimeoutExpired) as exc:
        return [f"error: {exc}"]
    return out.strip().splitlines()


def aligned_seq(fs: FleetSession, site_id: str) -> tuple[int, dict[str, object]]:
    """G2-7 시험 정렬 값(코디네이터 확정): N = max(c1 덤프 때 `/fleet` 요약의 `last_heartbeat_seq`,
    `last_report_seq`) + 1. 그 요약이 없으면 추정하지 않고 `E2EError`(시험 오류). (N, 기록)."""
    summary = fs.c1_fleet.get(site_id, {})
    marks = {key: summary.get(key) for key in ("last_heartbeat_seq", "last_report_seq")}
    if any(not isinstance(value, int) or isinstance(value, bool) for value in marks.values()):
        raise E2EError(
            f"no c1 seq marks for {site_id} in the central dump summary: {marks} "
            "(fleet/session.json c1_fleet) — cannot align the c0 report_seq counter"
        )
    next_seq = max(judges.as_int(v) for v in marks.values()) + 1
    return next_seq, {
        "next_seq": next_seq,
        "source": f"fleet/session.json c1_fleet[{site_id}] (central dump summary)",
        **marks,
        "reason": (
            "test alignment: the c0 VM counter restarts below the c1 central marks (c1 went "
            "online); a real airgap site has central marks 0"
        ),
    }


def airgap(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """G2-7(모듈 설명). 기록은 site-b `Context`에(USB 시각·seq 정렬·카운터·반입 응답)."""
    sc = scenario or Scenario.default()
    remote = sc.remote
    restored = sc.restore_airgap(session, fs, AIRGAP_SITE, remote=remote)
    run = g2.site_up(session, AIRGAP_SITE)
    ctx = run.ctx
    host = ctx.site.host()
    sh = remote.shell(host)
    pair = session.pair
    next_seq, seq_note = aligned_seq(fs, AIRGAP_SITE)
    site.align_seq(sh, AIRGAP_SITE, next_seq, sleep=remote.sleep)
    applied = judges.updater_state(remote.probe(host)).get("applied_gen")
    if judges.as_int(applied) < 1:
        raise InfraError(f"cannot read applied gen of {AIRGAP_SITE}: {applied!r}")
    gen = publish_gen(fleet_status(remote, AIRGAP_SITE).get("max_published_gen"), applied)
    start = (remote.now() - IMMEDIATE).replace(microsecond=0)
    record: dict[str, object] = {}
    notes: dict[str, object] = {}
    ops = remote.usb_ops
    remote.nft(host, airgap_filter())
    try:
        pib = sc.build(ctx, gen, pair.next, (start, start + WINDOW_LEN), session.tree_of(pair.next))
        with usb.image(ctx.run_id, AIRGAP_SITE, "airgap", [pib], record, ops=ops) as stick:
            with usb.attached(sh, ctx.site.vm_name, stick, record, ops=ops) as mount:
                done = site.busy_retry(sh, f"sudo pureiron-site apply {mount}/{pib.name}")
                notes["apply"] = {"rc": done.returncode, "stderr": done.stderr[-TAIL_CHARS:]}
                snap, waited = remote.converge(host, gen)
                usb.remount_rw(sh)
                out = site.busy_retry(
                    sh, f"sudo pureiron-site report export --out {mount}/{REPORTS_DIR}"
                )
                notes["export"] = {"rc": out.returncode, "stdout": out.stdout[-TAIL_CHARS:]}
                if out.returncode != 0:
                    raise E2EError(f"report export exited {out.returncode}: {out.stderr[-800:]}")
            counters = remote.table_counters(host, AIRGAP_NFT)
            if any(counters.get(name) != 0 for name in AIRGAP_COUNTERS):
                notes["airgap_log"] = airgap_log(sh)
            files = usb.read_dir(stick, REPORTS_DIR, ctx.work / "airgap", ops=ops)
            wrote = out.stdout.count(EXPORTED_MARK)
            if len(files) != wrote:  # debugfs는 rdump가 실패해도 0으로 끝난다(리뷰 M3)
                raise InfraError(
                    f"USB {stick.vhdx.name} not read: report export wrote {wrote} bundle(s), "
                    f"{len(files)} came out of /{REPORTS_DIR}"
                )
    finally:
        notes["filter_cleared"] = clear_filter(remote, host, ctx, AIRGAP_CLEAR)
    imports = [
        fleet_session.central(remote, "offline-import", "--file", golden.wsl_path(f)) for f in files
    ]
    row = fleet_status(remote, AIRGAP_SITE)
    ui = ui_fleet(remote)
    checks = judges.judge_airgap(
        counters,
        snap,
        imports,
        row,
        ui,
        site_id=AIRGAP_SITE,
        gen=gen,
        next_digests=ctx.digests[pair.next],
        want_counters=AIRGAP_COUNTERS,
    )
    ok = all(checks.values())
    ctx.record(
        ITEM_G2_7,
        ok,
        checks=checks,
        gen=gen,
        restored=restored,
        seq_alignment=seq_note,
        usb=record,
        counters=counters,
        exported=[f.name for f in files],
        imports=[{k: i.get(k) for k in ("status", "body", "file")} for i in imports],
        last_attempt=row.get("last_attempt"),
        current=row.get("current"),
        ui_last_attempt=judges.fleet_row(ui, AIRGAP_SITE).get("last_attempt"),
        converge_s=round(waited, 1),
        **notes,
    )
    return checks
