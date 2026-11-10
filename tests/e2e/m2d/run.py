"""M2d Hyper-V VM 시나리오(plan M2 종료 조건, PD-24). Windows 호스트에서 실행한다.

    uv run python tests/e2e/m2d/run.py all                # 처음부터 끝까지
    uv run python tests/e2e/m2d/run.py all --from crash   # 앞 단계 결과(context)를 이어서
    uv run python tests/e2e/m2d/run.py destroy            # VM·차분 디스크 삭제(golden은 둔다)

이 파일은 단계 순서·결과 항목·CLI만 가진 진입점이다. 원격 조작·판정·WSL 빌드는 공용 하네스
`tests/e2e/gates/harness/`(`site`·`judges`·`buildhost`·`trees`·`context`)에 있다. 현장은 `SITE_A`
하나(`site-a`, VM `pureiron-site-a`, 10.77.0.10, 상태 파일 `C:\\PureIron_e2e\\state.json`)다.

release 쌍은 `release_pair.DEFAULT`(`BASE`→`NEXT`, 헬스 실패 `FAULT`)가 정한다 — M4 마감:
r4(epoch 5)→r5(epoch 5, 현장 마이그레이션 없음), fault r5'(`r5f`). 아래의 BASE·NEXT·FAULT는 그
값이다.

단계(`STEPS`):
1. `prepare` — WSL runner의 저장소 사본에서 FAULT 기록, 설치 번들(실빌드: deb 수집·Updater
   venv), g1(BASE) 현장 번들. release 기록의 epoch가 쌍의 `epochs`와 다르면 멈춘다.
2. `vm` — golden VHDX 위 차분 디스크 + seed ISO로 `pureiron-site-a`(2 vCPU/`PUREIRON_E2E_VM_GIB`
   GiB, 기본 3, 내부 전용 망).
3. `install` — runbook 2단계(전용 디렉터리에 메타데이터만 추출 → 지문·sha256 대조 →
   `bootstrap-verify.sh`) → `install.sh --offline-time-confirmed` → enroll → g1 apply → 서비스 시작.
   `/readyz` 200, classic image store(overlay2), 이미지 `.Id` == BASE config digest. 압축 chunk가
   있는 이력을 넣고(DB epoch == BASE epoch·BASE 산출물 있음·NEXT 테이블 없음 확인) 체크포인트
   `c0`를 만든다.
4. `scenarios` — `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1과 같은 gen 재빌드 no-op
   exit 0(R-7 멱등), 모두 core `StartedAt`·실행 이미지 불변. `c0`에서 BASE→NEXT(압축 DB 위 NEXT
   epoch migrate, 번들 트리 = NEXT 트리 — fixture에서 태그 `ReleasePair.tree_tag` description만
   바꾼 것이라 core가 정의를 다시 쓴다) 성공 + NEXT 스키마 확인 → 그 위에서 낮은 gen `E_GEN` 거부.
   이어서 M5 확인 4개(`step_m5`): ⓐ NEXT 트리 적용(`project_files`·정의 값·c0 시드 retire·
   `site_rev == base_site_rev`) ⓑ web 경유 admin
   로그인 → 태그·알람 운영 값 PATCH(site_rev +2, DB 정의 일치) ⓒ absorb하지 않은 repo로 만든 번들
   `E_DRAFT` 거부 ⓓ `pureiron-site project export` → 호스트 → WSL `absorb`·`gate` → 그 트리 번들
   적용(`base_site_rev == site_rev`, PATCH 값 유지). `c0`에서 FAULT → 5분 안에 BASE digest 복귀 +
   `failed`. BASE(r4)도 트리를 적용하는 release라 트리 백업이 있는 일반 롤백이다(경보 없음,
   메트릭 `updater_failed_manual 0`): FAULT 번들은 fixture·NEXT와 다른 트리(FAULT 트리)를 실어서
   롤백 뒤 BASE 트리·정의가 되돌아왔는지(content_hash·DB 태그 정의 값 = fixture) 보인다. 그
   상태에서 정상 NEXT 번들(NEXT 트리)로 다시 올리면 `succeeded`(정의 값 = NEXT 트리).
5. `crash` — phase마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Force`를 1회씩.
   서비스가 그 시도를 실행하도록 창이 조금 뒤에 열리는 번들을 CLI로 스테이징하고(`verified` 대기),
   VM 쪽 `crash_at.py`가 해당 phase 진입 순간 프로세스를 얼린 뒤 kill/리셋한다. `migrating`은
   실제 마이그레이션이 일어나도록 `c0`에서 BASE→NEXT, `rolling_back`은 FAULT로, 나머지는
   NEXT↔BASE를 오간다. `fetched`(번들 검증)는 CLI를 대상으로. 매번 10분 안에 `succeeded`
   (rolling_back은 `failed`) + 실행 이미지 == 기대 release + 편집 잠금 해제로 수렴해야 한다.
6. `online`(M4 마감 ★D5) — `c0`(BASE g1, 폐쇄망 상태)로 복원한 뒤 WSL에 중앙 c0(`central.py`,
   `deploy/central` compose, `10.77.0.1:28443` HTTPS·Caddy 사설 CA)를 새로 띄우고 현장을 온라인으로
   바꾼다: 루트 CA를 OS 신뢰 저장소에 넣고(지문 대조) `updater.env`에 `PUREIRON_CONTROL_API`, VM
   enrollment로 등록한 현장 token을 secret 파일에, Updater 재시작. 4항목: ① HTTPS heartbeat →
   `/fleet` online ② 중앙에 PUT한 BASE g2 → 다운로드·적용 `succeeded` ③ 온라인 보고 → `/fleet`
   current·last_attempt(`source` online) ④ control-web(Caddy)만 멈춤 → 현장 정상·Updater 경보
   없음, 3 x poll 뒤 `/fleet` stale → 재기동 뒤 online. 중앙은 끝에 내린다(`--keep-central`이면
   남긴다 — 15분 실측이 그대로 이어 쓴다, 정리는 `run.py central-down`).
7. `report` — nftables 외부 접속 카운터 0 확인(online 구간 포함 — VM → 10.77.0.1은 내부 망이라
   `ext_out_v4` 제외 대상, IP 직접 접속이라 DNS 질의 없음), `bundle-metrics.json`, `results.json`
   (빠진 항목은 `missing`).

결과: `artifacts/e2e/m2d-<시각>/`(gitignore). 진행 상태는 `C:\\PureIron_e2e\\state.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gates"))

from harness import buildhost, golden, hyperv, judges, paths, site, sites, trees
from harness.context import TAIL_CHARS, Context, E2EError, log
from harness.judges import (
    FAULT_CODE,
    ONLINE_GEN,
    as_int,
    db_of,
    definitions,
    judge_attempt,
    judge_tree,
    project_body,
    reply,
    revs,
    updater_state,
)
from harness.release_pair import DEFAULT, ReleasePair
from harness.site import INCOMING, REMOTE
from harness.sites import CLOUD_IMAGE, GOLDEN, SITE_A, SWITCH, site_vm_spec, vm_memory_gib
from harness.vm import Vm

PAIR = DEFAULT
BASE, NEXT, FAULT = PAIR.base, PAIR.next, PAIR.fault
__all__ = ["CLOUD_IMAGE", "GOLDEN", "SITE_A", "SWITCH", "site_vm_spec", "vm_memory_gib"]
"""현장 정의·VM 준비는 `harness.sites`로 옮겼다 — 시험·도구가 쓰던 `run.<이름>`은 그대로 둔다."""
OTHER_SITE = "site-x"
"""거부 시험("reject site")의 다른 현장 id — G2의 실제 현장(site-a·site-b)과 겹치지 않는다(M6-1
리뷰 L4)."""


INSTALL_RELEASE = PAIR.releases[BASE]
"""설치 번들 manifest `release`(이미지는 싣지 않는다 — 실행 release는 g1 현장 번들이 정한다)."""
WINDOW_LEAD_S = 150
CRASH_PHASES = (
    "fetched",
    "verified",
    "committed",
    "locked",
    "backed_up",
    "migrating",
    "switching",
    "checking",
    "rolling_back",
)
STEPS = ("prepare", "vm", "install", "scenarios", "crash", "online", "report")

# 결과 항목 이름 — release가 들어가는 것은 쌍에서 만든다. 전체 목록은 `expected_items()`.
ITEM_VM_BOOT = "vm boot (cloud-init, internal-only switch)"
ITEM_OS_QUIET = "OS quiet before install (nftables baseline)"
ITEM_INSTALL = f"install bundle bootstrap + g1({BASE}) on offline VM"
ITEM_REJECT_SITE, ITEM_REJECT_TAMPERED = "reject site", "reject tampered"
ITEM_SAME_GEN = "same gen rebuild → no-op"
ITEM_UPGRADE = f"{BASE}→{NEXT} on compressed epoch-{PAIR.epochs[BASE]} DB"
ITEM_SCHEMA = f"{NEXT} schema after upgrade"
ITEM_LOWER_GEN = "reject lower gen"
ITEM_TREE = f"{NEXT} project tree applied (project_files, definitions, base_site_rev)"
ITEM_PATCH = "site PATCH via web (admin login, tag + alarm) → site_rev +2"
ITEM_DRAFT_GATE = "reject unabsorbed site drafts (E_DRAFT)"
ITEM_ABSORB = "draft export → absorb → gate → reapply (PATCH kept, base_site_rev == site_rev)"
ITEM_FAULT = f"{NEXT}' health failure → rollback (no alarm, {BASE} tree and definitions back)"
ITEM_ROLLBACK_TIME = f"rollback within 5 min to {BASE} digests"
ITEM_RETRY = f"{NEXT} retry after {NEXT}' rollback → succeeded"
ITEM_ONLINE_POLL = "online: HTTPS heartbeat to central c0 (private CA trusted) → /fleet online"
ITEM_ONLINE_APPLY = f"online: central-published {BASE} g{ONLINE_GEN} → download + apply succeeded"
ITEM_ONLINE_REPORT = "online: ApplyAttempt report → /fleet current + last_attempt (source online)"
ITEM_ONLINE_OUTAGE = "online: central web down → site keeps running, /fleet stale → back online"
ITEM_NO_EXTERNAL = "no external connection attempts since install (nftables, accumulated)"
M5_ITEMS = (ITEM_TREE, ITEM_PATCH, ITEM_DRAFT_GATE, ITEM_ABSORB)
ONLINE_ITEMS = (ITEM_ONLINE_POLL, ITEM_ONLINE_APPLY, ITEM_ONLINE_REPORT, ITEM_ONLINE_OUTAGE)


@dataclass(frozen=True)
class CrashCase:
    phase: str
    mode: str
    """`kill`(서비스 SIGKILL) 또는 `reset`(VM 전원 리셋)."""
    release: str
    prev: str
    """주입 전 실행 중인 release — 실패(FAULT)면 이것으로 돌아가야 한다."""
    from_c0: bool = False
    """`c0`(BASE epoch, 압축 이력)로 복원한 뒤 주입한다 — 실제 마이그레이션이 일어나는 migrating."""

    @property
    def name(self) -> str:
        return f"crash {self.mode} @ {self.phase}"


def crash_plan(pair: ReleasePair) -> list[CrashCase]:
    """crash 주입 순서. migrating은 c0에서 BASE→NEXT(두 번 모두 복원 후), rolling_back은 FAULT,
    나머지 phase는 직전 결과에서 NEXT↔BASE를 번갈아 간다(c0 이후 첫 대상 = NEXT 위에서 BASE)."""
    base, nxt, fault = pair.base, pair.next, pair.fault
    cases = [CrashCase("migrating", mode, nxt, base, from_c0=True) for mode in ("kill", "reset")]
    current = nxt
    for phase in CRASH_PHASES:
        if phase == "migrating":
            continue
        for mode in ("kill", "reset"):
            if phase == "rolling_back":
                cases.append(CrashCase(phase, mode, fault, current))
            else:
                target = base if current == nxt else nxt
                cases.append(CrashCase(phase, mode, target, current))
                current = target
    return cases


def expected_items() -> list[str]:
    """`all` 한 번이 남기는 결과 항목 이름(39개, 순서대로) — 실측끼리 39/39로 비교한다. S4까지의
    30개에 M5 확인 4개(트리·PATCH·드래프트 게이트·absorb 재적용)와 롤백 뒤 재시도 1개(M5 마감의
    전환기 경보 해제 자리), M4 마감 온라인 4개를 더했다. 외부 접속 항목은 online 단계까지 덮도록
    맨 끝(report)에 둔다."""
    return [
        ITEM_VM_BOOT,
        ITEM_OS_QUIET,
        ITEM_INSTALL,
        ITEM_REJECT_SITE,
        ITEM_REJECT_TAMPERED,
        ITEM_SAME_GEN,
        ITEM_UPGRADE,
        ITEM_SCHEMA,
        ITEM_LOWER_GEN,
        ITEM_TREE,
        ITEM_PATCH,
        ITEM_DRAFT_GATE,
        ITEM_ABSORB,
        ITEM_FAULT,
        ITEM_ROLLBACK_TIME,
        ITEM_RETRY,
        *(case.name for case in crash_plan(PAIR)),
        *ONLINE_ITEMS,
        ITEM_NO_EXTERNAL,
    ]


# ---------------------------------------------------------------- prepare·VM·설치


def step_prepare(ctx: Context) -> None:
    log("prepare: WSL repo sync")
    buildhost.sync_repo()
    log(f"prepare: fault release {FAULT} (from {NEXT})")
    buildhost.wsl(buildhost.bundles_py("fault-release"))
    ctx.digests = {}
    for name in PAIR.releases:
        record = json.loads(buildhost.wsl(f"cat {paths.WSL_HOME}/work/releases/{name}.json"))
        judges.check_record(PAIR, name, record)
        ctx.digests[name] = judges.digests_of(record)
    log("prepare: install bundle (fetch_debs, build_updater, build_install_bundle)")
    pib, fpr, boot = buildhost.install_bundle(ctx.work, INSTALL_RELEASE)
    ctx.install_pib, ctx.runbook_fpr, ctx.runbook_bootstrap = str(pib), fpr, boot
    ctx.bundles.append({"kind": "install", "file": pib.name, "size_bytes": pib.stat().st_size})
    log(f"prepare: g1 ({BASE}) site bundle")
    buildhost.build_site_bundle(ctx, 1, BASE, datetime.now(UTC) - timedelta(days=1))
    ctx.save()


def step_vm(ctx: Context) -> None:
    spec = site_vm_spec(ctx.site, os.environ)
    sites.check_host()
    golden.build_golden(CLOUD_IMAGE, GOLDEN)
    pub = golden.ensure_ssh_key(paths.SSH_DIR).read_text(encoding="utf-8")
    files = golden.seed_files(
        ctx.site.site_id,
        ctx.site.ip,
        pub,
        golden.Network(),
        field=trees.field_endpoints(trees.FIXTURE),
    )
    waited = sites.boot_vm(ctx.site, spec, files)
    host = ctx.site.host()
    ctx.nft_before = site.nft_counters(host)
    ctx.nft_last = dict(ctx.nft_before)
    ctx.nft_total = dict.fromkeys(ctx.nft_before, 0)
    ctx.nft_mark_at, boot_lookups = site.resolved_lookups(host, 0, ctx.site.site_id)
    ctx.dns_lookups = {}
    ctx.record(ITEM_VM_BOOT, True, ssh_after_s=round(waited, 1))
    ctx.record(
        ITEM_OS_QUIET,
        judges.no_external(ctx.nft_before),
        baseline=ctx.nft_before,
        dns_lookups=boot_lookups,
    )


def step_install(ctx: Context) -> None:
    host = ctx.site.host()
    install = Path(ctx.install_pib)
    g1 = buildhost.site_bundle(ctx, 1, BASE)
    install_s = site.install_runbook(
        host,
        install,
        g1,
        site_id=ctx.site.site_id,
        runbook_fpr=ctx.runbook_fpr,
        runbook_bootstrap=ctx.runbook_bootstrap,
    )
    snap = site.wait_ready(host, 300)
    seen = judges.judge_installed(snap, ctx.digests[BASE])
    installed = all(seen.values())
    c0: dict[str, object] = {}
    if installed:  # 이력 시드 → c0 기반(BASE epoch + 압축 chunk 이력) 확인
        site.seed_history(host)
        c0 = db_of(site.probe(host))
        ctx.seed_rows = as_int(c0.get("tag_history_rows"))
    c0_checks = judges.judge_c0(c0, PAIR)
    ok = installed and all(c0_checks.values())
    ctx.record(
        ITEM_INSTALL,
        ok,
        install_s=round(install_s, 1),
        storage_driver=cast(dict[str, object], snap.get("docker") or {}).get("driver"),
        classic_store=seen["classic_store"],
        release=BASE,
        images_match=seen["images_match"],
        readyz=cast(dict[str, object], snap.get("readyz") or {}).get("status"),
        c0_checks=c0_checks,
        c0_db=c0,
    )
    if not ok:  # 잘못된 기반 위에 c0를 찍고 이어 가지 않는다
        raise E2EError("install failed — see results")
    host.run("sudo rm -f " + f"{INCOMING}/{install.name} {INCOMING}/{g1.name}")
    site.nft_mark(ctx, host)
    ctx.nft_c0 = dict(ctx.nft_last)
    hyperv.checkpoint(ctx.site.vm_name, "c0")
    ctx.current = BASE
    ctx.save()


# ---------------------------------------------------------------- 시나리오·M5 확인


def scenario_gens(gen: int) -> dict[str, int]:
    """`scenarios`가 쓰는 generation(`gen` = 시작 시 `ctx.next_gen`). NEXT 줄(업그레이드 → ⓒ → ⓓ)은
    차례로 올라가고, FAULT·재시도는 c0(applied g1) 복원 뒤라 업그레이드와 같은 gen부터 다시 쓸 수
    있지만 재시도는 번들 파일 이름(`g<gen>-<release>`)이 NEXT 줄과 겹치지 않게 그 뒤로 둔다.
    `next`는 crash 단계의 시작 gen."""
    return {
        "upgrade": gen,
        "draft_gate": gen + 1,
        "absorb": gen + 2,
        "fault": gen,
        "retry": gen + 3,
        "next": gen + 4,
    }


def step_m5(ctx: Context, gens: dict[str, int]) -> None:
    """업그레이드(NEXT, NEXT 트리) 위 M5 확인 ⓐ~ⓓ. gen은 `scenario_gens`(ⓒ 거부 `draft_gate`,
    ⓓ 적용 `absorb`). ⓑ PATCH 대상·ⓒ·ⓓ의 fixture 경로는 NEXT 트리와 무관하다(NEXT 트리는
    `tree_tag` description만 다르다 — ⓓ absorb는 그 값도 PATCH 허용 필드라 드래프트에서 함께
    옮긴다)."""
    host = ctx.site.host()
    # ⓐ NEXT 트리 적용(정의 값·c0 시드 retire까지)
    snap = site.probe(host)
    tree = judge_tree(snap, trees.tree_expect(buildhost.release_tree(ctx, NEXT), PAIR))
    db = db_of(snap)
    ctx.record(
        ITEM_TREE,
        all(tree.values()),
        checks=tree,
        project=project_body(snap),
        db={k: db.get(k) for k in ("project_files", "device_ids", "tag_paths", "alarm_ids")},
        tree_tag_def=definitions(db, "tag_defs").get(PAIR.tree_tag),
    )
    # ⓑ web 경유 admin 로그인 → 태그·알람 운영 값 PATCH
    out, admin = site.site_patch(host)
    after = db_of(site.probe(host))
    patch_checks = {"admin_created": admin["rc"] == 0, **judges.judge_patch(out, after)}
    ctx.record(
        ITEM_PATCH,
        all(patch_checks.values()),
        checks=patch_checks,
        admin=admin,
        replies=out,
        tag_def=definitions(after, "tag_defs").get(judges.PATCH_TAG),
        alarm_def=definitions(after, "alarm_defs").get(judges.PATCH_ALARM),
    )
    patched = revs(reply(out, "project_after")[1])
    want_rev = patched[0] if patched is not None else -1
    # ⓒ absorb하지 않은 repo(fixture에서 absorbs만 뺀 사본)로 만든 번들 → E_DRAFT
    repo = buildhost.wsl(buildhost.bundles_py("m5-project"))
    unabsorbed = buildhost.build_site_bundle(
        ctx,
        gens["draft_gate"],
        NEXT,
        datetime.now(UTC) - timedelta(minutes=5),
        repo.strip().splitlines()[-1],
    )
    site.stage_untouched(
        ctx, ITEM_DRAFT_GATE, host, unabsorbed, want_exit=1, want_code="E_DRAFT", want=NEXT
    )
    # ⓓ export → 호스트 → WSL absorb·gate → 흡수한 트리로 번들 → 적용
    reapply_absorbed(ctx, host, gens["absorb"], want_rev)


def reapply_absorbed(ctx: Context, host: Vm, gen: int, want_rev: int) -> None:
    export = host.run(
        site.as_root(
            f"pureiron-site project export --out {site.DRAFT_REMOTE} && "
            f"chown pureiron: {site.DRAFT_REMOTE} && "
            f"install -o pureiron -m 0644 {site.STATE_DIR}/enrollment.json "
            f"{site.ENROLLMENT_REMOTE}"
        ),
        check=False,
    )
    note: dict[str, object] = {
        "export": {
            "rc": export.returncode,
            "stdout": export.stdout[-TAIL_CHARS:],
            "stderr": export.stderr[-TAIL_CHARS:],
        }
    }
    if export.returncode != 0:
        ctx.record(ITEM_ABSORB, False, **note)
        return
    local = ctx.work / "m5"
    draft, enrollment = local / "draft.tar", local / "enrollment.json"
    host.get(site.DRAFT_REMOTE, draft)
    host.get(site.ENROLLMENT_REMOTE, enrollment)
    raw = buildhost.wsl(
        f"{buildhost.bundles_py('absorb')} "
        f"--draft '{golden.wsl_path(draft)}' --site-key '{golden.wsl_path(enrollment)}' "
        f"--site {ctx.site.site_id}"
    )
    result = cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    note["absorb"] = result
    absorb_checks = judges.judge_absorb(result, want_rev, ctx.site.site_id)
    if not all(absorb_checks.values()):
        ctx.record(ITEM_ABSORB, False, checks=absorb_checks, **note)
        return
    content_hash = judges.absorbed_hash(result)
    site.apply_and_check(
        ctx,
        ITEM_ABSORB,
        gen,
        NEXT,
        expect="succeeded",
        prev=NEXT,
        project_dir=str(result["dir"]),
        extra=lambda snap: {
            **absorb_checks,
            **judges.judge_reapplied(snap, want_rev, content_hash),
        },
        note=note,
    )


def m5_guarded(
    ctx: Context, gens: dict[str, int], step: Callable[[Context, dict[str, int]], None] = step_m5
) -> None:
    """M5 확인을 돌리되 전송·WSL 예외(`VmError`·golden.wsl `RuntimeError`·`ValueError` 등)가
    `all` 전체를 끊지 않게 한다 — 이번 호출에서 기록되지 않은 M5 항목을 실패(`error` = 예외 끝)로
    남기고 돌아가, 호출자가 `restore_c0`부터 이어 간다. 이전 실행이 남긴 같은 이름 결과는 이번
    기록으로 치지 않는다."""
    before = list(ctx.results)
    try:
        step(ctx, gens)
    except Exception as exc:
        recorded = {r["name"] for r in ctx.results if not any(r is b for b in before)}
        for name in M5_ITEMS:
            if name not in recorded:
                ctx.record(name, False, error=repr(exc)[-TAIL_CHARS:])


def step_scenarios(ctx: Context) -> None:
    gens = scenario_gens(ctx.next_gen)
    gen = gens["upgrade"]
    # 거부 코드(VM 수동 1회): 다른 현장, 변조 — c0(applied g1 = BASE) 위에서
    other = ctx.work / f"pureiron-{OTHER_SITE}-g{gen}.pib"
    buildhost.wsl(
        f"{buildhost.bundles_py('site')} "
        f"--out '{golden.wsl_path(other.parent)}' --gen {gen} --product {PAIR.releases[NEXT]} "
        "--window-start 2026-01-01T00:00:00Z --window-end 2027-01-01T00:00:00Z "
        f"--site {OTHER_SITE}"
    )
    # 같은 gen(1) 재빌드(NEXT) — 내용이 달라도 gen 규칙상 멱등 no-op(R-7).
    same = buildhost.build_site_bundle(ctx, 1, NEXT, datetime.now(UTC) - timedelta(days=1))
    tampered = same.with_name(f"pureiron-{ctx.site.site_id}-g{gen}-tampered.pib")
    data = bytearray(buildhost.build_site_bundle(ctx, gen, NEXT, datetime.now(UTC)).read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    site.restore_c0(ctx)
    host = ctx.site.host()
    for name, pib, code in (
        (ITEM_REJECT_SITE, other, "E_SITE"),
        (ITEM_REJECT_TAMPERED, tampered, "E_DIGEST"),
    ):
        site.stage_untouched(ctx, name, host, pib, want_exit=1, want_code=code, want=BASE)
    site.stage_untouched(ctx, ITEM_SAME_GEN, host, same, want_exit=0, want_code=None, want=BASE)

    # c0(BASE epoch, 압축 chunk)에서 BASE→NEXT — 압축 DB 위에서 NEXT 마이그레이션이 실제로 돈다
    site.restore_c0(ctx)
    upgrade_next(ctx, gen)
    # applied g2 위에서 g1 → E_GEN(낮은 gen). g2보다 뒤에 빌드해야 한다 — 먼저 만든 번들은
    # cert 단계의 `max_seen_created_at` 역행(E_CERT)에 먼저 걸린다(첫 실측).
    lower = buildhost.build_site_bundle(ctx, 1, NEXT, datetime.now(UTC) - timedelta(days=1))
    site.stage_untouched(
        ctx, ITEM_LOWER_GEN, ctx.site.host(), lower, want_exit=1, want_code="E_GEN", want=NEXT
    )
    m5_guarded(ctx, gens)
    site.restore_c0(ctx)
    fault_and_retry(ctx, gens)
    ctx.next_gen = gens["next"]
    ctx.save()


Apply = Callable[..., dict[str, object]]
"""`site.apply_and_check`와 같은 호출 모양 — 배선 단위 시험이 가짜로 바꿔 끼운다."""


def upgrade_next(ctx: Context, gen: int, apply: Apply = site.apply_and_check) -> None:
    """c0에서 BASE→NEXT(ITEM_UPGRADE) + NEXT 스키마(ITEM_SCHEMA). 번들 트리는 NEXT 트리 — c0의
    fixture 트리와 같으면 core가 재적용을 건너뛰어 ⓐ(ITEM_TREE)가 c0 DB(시드 남음)를 보고
    실패한다(H1)."""
    up = apply(
        ctx,
        ITEM_UPGRADE,
        gen,
        NEXT,
        expect="succeeded",
        prev=BASE,
        project_dir=golden.wsl_path(buildhost.release_tree(ctx, NEXT)),
    )
    db = db_of(cast(dict[str, object], up["snapshot"]))
    schema_checks = judges.judge_next_schema(db, PAIR)
    ctx.record(ITEM_SCHEMA, all(schema_checks.values()), checks=schema_checks, db=db)


def fault_and_retry(
    ctx: Context, gens: dict[str, int], apply: Apply = site.apply_and_check
) -> None:
    """c0에서 FAULT → 5분 안에 BASE 복귀 + failed, 이어서 NEXT 재시도 → succeeded.

    BASE(r4)도 트리 release라 Updater 백업에 트리가 있고 롤백이 트리·정의까지 되돌린다 — 경보
    없음(전환기 E_DEFINITIONS_LEFT는 r3→r4에서만, 단위 시험이 고정). FAULT 번들은 fixture와 다른
    FAULT 트리(c0에는 드래프트가 없어 게이트 통과)를 싣는다 — 롤백 뒤 content_hash와 태그 정의
    값이 fixture(c0)로 돌아왔는지가 판정에 보인다. 재시도는 NEXT 트리라 롤백된 fixture 상태에서
    정의를 다시 쓴다(정의 값 = NEXT 트리) — fixture를 실으면 no-op이라 늘 참인 판정이 된다."""
    fault_tree = buildhost.release_tree(ctx, FAULT)
    next_tree = buildhost.release_tree(ctx, NEXT)

    def fixture_tree_back(snap: dict[str, object]) -> dict[str, bool]:
        return trees.judge_fixture_tree(snap, PAIR)

    rb = apply(
        ctx,
        ITEM_FAULT,
        gens["fault"],
        FAULT,
        expect="failed",
        prev=BASE,
        project_dir=golden.wsl_path(fault_tree),
        expect_code=FAULT_CODE,
        check_metric=True,
        extra=fixture_tree_back,
    )
    ctx.record(
        ITEM_ROLLBACK_TIME,
        judges.judge_rollback_time(rb),
        seconds=rb["seconds"],
        code=rb["code"],
    )
    next_expect = trees.tree_expect(next_tree, PAIR)

    def next_tree_applied(snap: dict[str, object]) -> dict[str, bool]:
        return judge_tree(snap, next_expect)

    apply(
        ctx,
        ITEM_RETRY,
        gens["retry"],
        NEXT,
        expect="succeeded",
        prev=BASE,
        project_dir=golden.wsl_path(next_tree),
        check_metric=True,
        extra=next_tree_applied,
    )


# ---------------------------------------------------------------- crash 주입


def crash_once(
    ctx: Context, case: CrashCase, *, gen: int, note: dict[str, object] | None = None
) -> dict[str, object] | None:
    """`case`를 한 번 주입하고 `case.name`으로 기록한다(`note`는 결과에 함께 남길 값). 창 여유 부족
    flake(E10, `judges.judge_window_flake`)면 그 시도의 증거를, 아니면 None을 돌려준다."""
    host = ctx.site.host()
    phase, mode, release = case.phase, case.mode, case.release
    expect = "failed" if release == FAULT else "succeeded"
    # 창은 번들에 서명되므로 빌드 전에 정한다. service 대상이면 CLI 스테이징이 창 전에 끝났는지
    # 확인한다 — 아니면 CLI가 phase를 직접 실행해 쉬고 있는 서비스를 죽이는 거짓 통과가 된다.
    open_at = datetime.now(UTC) + timedelta(seconds=WINDOW_LEAD_S)
    pib = buildhost.build_site_bundle(ctx, gen, release, open_at)
    host.put(pib, f"{INCOMING}/{pib.name}")
    applied_before = updater_state(site.probe(host)).get("applied_gen")
    boot_before, restarts_before = site.boot_id(host), site.service_restarts(host)
    target = "cli" if phase == "fetched" else "service"
    watch_mode = "kill" if mode == "kill" else "freeze"
    watcher = host.start(
        f"sudo python3 {REMOTE}/crash_at.py --generation {gen} --phase {phase} "
        f"--mode {watch_mode} --target {target} --timeout {WINDOW_LEAD_S + 600}"
    )
    time.sleep(2)
    apply_cmd = f"sudo pureiron-site apply {INCOMING}/{pib.name}"
    staged_in_time: bool | None = None
    stage_proc: subprocess.Popen[str] | None = None
    stage_info: dict[str, object] = {"target": target, "ssh": shutil.which("ssh")}
    if target == "cli":
        stage_proc = host.start(apply_cmd, capture=False)
    else:
        staged_in_time, info = site.stage_for_window(
            lambda: host.run(apply_cmd, check=False),
            open_at,
            watcher_alive=lambda: watcher.poll() is None,
        )
        stage_info.update(info)
    line = watcher.stdout.readline().strip() if watcher.stdout else ""
    # 감시자는 HIT를 출력하고 곧 끝난다 — 리셋이 ssh를 끊기 전에 종료 코드를 받는다(rc 5 = 얼릴
    # 프로세스 없음).
    watcher_rc = watcher.wait(timeout=60)
    hit = line.startswith("HIT") and watcher_rc == 0
    if hit and mode == "reset":
        site.nft_mark(ctx, host)  # 리셋은 카운터를 0으로 되돌린다
        hyperv.restart_force(ctx.site.vm_name)
        ctx.nft_last = {}
    if stage_proc is not None:
        stage_info["rc"] = stage_proc.wait(timeout=1800)  # CLI 대상은 감시자가 죽인다
    snap, waited = site.converge(ctx.site.host(), gen)
    report = site.report_for(ctx.site.host(), pib)
    want = ctx.digests[release if expect == "succeeded" else case.prev]
    checks = judge_attempt(
        snap,
        report,
        expect=expect,
        want_digests=want,
        want_applied=gen if expect == "succeeded" else cast(int | None, applied_before),
    )
    checks["hit"] = hit
    if staged_in_time is not None:
        checks["staged_before_window"] = staged_in_time
    # 주입 효과: 리셋이면 boot id가 바뀌고, 서비스 kill이면 systemd 재시작 횟수가 는다.
    if mode == "reset":
        checks["rebooted"] = site.boot_id(ctx.site.host()) != boot_before
    elif target == "service":
        checks["service_restarted"] = site.service_restarts(ctx.site.host()) > restarts_before
    rows = as_int(db_of(snap).get("tag_history_rows"))
    checks["rows_kept"] = rows >= ctx.seed_rows > 0
    ctx.record(
        case.name,
        all(checks.values()),
        gen=gen,
        release=release,
        watcher=line,
        result=updater_state(snap).get("last_result"),
        code=report.get("code"),
        converge_s=round(waited, 1),
        checks=checks,
        stage=stage_info,
        alarm=updater_state(snap).get("alarm"),
        logs=report.get("logs") if not all(checks.values()) else None,
        **(note or {}),
    )
    ctx.site.host().run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)
    if judges.judge_window_flake(checks, stage_info):
        return {"gen": gen, "checks": checks, "stage": stage_info}
    return None


CrashOnce = Callable[..., dict[str, object] | None]
"""`crash_once`와 같은 호출 모양 — 재시도 배선 단위 시험이 가짜로 바꿔 끼운다."""
Settle = Callable[[Context, int, str], None]


def settle_release(ctx: Context, gen: int, release: str) -> None:
    """crash 없이 g`gen` `release` 번들을 적용해 그 release로 돌려놓는다(E10 재시도 준비 — 결과
    항목을 남기지 않는다). 수렴하지 않으면 멈춘다."""
    host = ctx.site.host()
    pib = buildhost.build_site_bundle(ctx, gen, release, datetime.now(UTC) - timedelta(minutes=5))
    site.stage(host, pib, check=False)
    snap, _ = site.converge(host, gen)
    state = updater_state(snap)
    if state.get("last_result") != "succeeded" or (
        judges.running_digests(snap) != ctx.digests[release]
    ):
        raise E2EError(f"settle g{gen} {release} failed: {state.get('last_result')}")
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def retry_window_flake(
    ctx: Context,
    case: CrashCase,
    flake: dict[str, object],
    *,
    c0_gen: int,
    gen: int,
    crash: CrashOnce,
    settle: Settle,
    restore: Callable[[Context], None],
) -> int:
    """E10: 스테이징이 창 뒤에 끝났지만(`margin_s <= 0`) 제품 판정은 모두 참이었던 crash 건을
    `infra`로 남기고 1회 다시 한다. 창은 번들에 서명되므로 다시 빌드한다. 같은 출발 상태가 되게
    from_c0 건은 c0로 복원해 같은 gen으로, FAULT가 아닌 건은 `case.prev`로 먼저 되돌린 뒤(gen 하나
    소비) 다음 gen으로 한다(FAULT는 이미 `case.prev`로 롤백돼 있다). 재시도도 같으면 그 결과
    (실패)가 남는다 — 두 번 다시 하지 않는다. 다음 gen을 돌려준다."""
    note: dict[str, object] = {"e10": {"class": "infra", "first": flake}}
    ctx.log(f"crash: {case.name} staged after the window opened (infra) - retrying once")
    if case.from_c0:
        restore(ctx)
        crash(ctx, case, gen=c0_gen, note=note)
        return gen
    if case.release != FAULT:
        settle(ctx, gen, case.prev)
        gen += 1
    crash(ctx, case, gen=gen, note=note)
    return gen + 1


def step_crash(
    ctx: Context,
    *,
    crash: CrashOnce = crash_once,
    settle: Settle = settle_release,
    restore: Callable[[Context], None] = site.restore_c0,
) -> None:
    # migrating은 c0(BASE epoch)로 복원해 같은 gen으로 두 번, 나머지는 하나씩 gen을 올린다.
    c0_gen = ctx.next_gen
    gen = c0_gen + 1
    plan = crash_plan(PAIR)
    for case in plan:
        if case.from_c0:
            restore(ctx)
            flake = crash(ctx, case, gen=c0_gen)
        else:
            flake = crash(ctx, case, gen=gen)
            gen += 1
        if flake is not None:
            gen = retry_window_flake(
                ctx,
                case,
                flake,
                c0_gen=c0_gen,
                gen=gen,
                crash=crash,
                settle=settle,
                restore=restore,
            )
    last = plan[-1]
    ctx.next_gen, ctx.current = gen, last.prev if last.release == FAULT else last.release
    ctx.save()


# ---------------------------------------------------------------- 온라인(M4 마감 ★D5)

OUTAGE_WAIT_S = 3 * site.CENTRAL_POLL_S + 15
"""control-web을 멈추고 `/fleet` stale을 보기까지 기다리는 시간(3 x poll + 여유)."""
ONLINE_POLL_ROW_CHECKS = ("connection_online", "heartbeat_seq", "current_g1")
"""`judge_online_poll`에서 `/fleet` 행으로 정해지는 항목. CA 지문·등록은 행과 무관해 기다려도
안 바뀐다 — 대기 조건에서 빼도 판정(전체 항목)은 같다."""


def online_outage(ctx: Context, host: Vm) -> None:
    """④ control-web(Caddy)만 멈춘다 — 현장이 닿는 유일한 입구(control-api 직접 포트는 없다, F8)."""
    site_id = ctx.site.site_id
    before = site.probe(host)
    restarts_before = site.service_restarts(host)
    buildhost.central("stop-web")
    stopped = time.monotonic()
    try:
        time.sleep(OUTAGE_WAIT_S)
        during = site.probe(host)
        restarts_after = site.service_restarts(host)
        active = site.updater_active(host)
        stale = judges.fleet_row(buildhost.central("fleet", "--site", site_id, "--direct"), site_id)
        waited_s = round(time.monotonic() - stopped, 1)
    finally:
        buildhost.central("start-web")
    back, back_s = buildhost.wait_fleet(
        site_id, lambda r: r.get("connection") == "online" and judges.seen_after(r, stale)
    )
    checks = judges.judge_outage(
        during,
        want_digests=ctx.digests[BASE],
        stale=stale,
        back=back,
        updater_active=active,
        restarts=(restarts_before, restarts_after),
        core_started=(judges.core_started_at(before), judges.core_started_at(during)),
    )
    ctx.record(
        ITEM_ONLINE_OUTAGE,
        all(checks.values()),
        checks=checks,
        stopped_s=waited_s,
        back_online_s=back_s,
        stale_row=stale,
        back_row=back,
        updater=updater_state(during),
    )


def online_poll(ctx: Context, setup: dict[str, object]) -> None:
    """① `/fleet` 행이 판정의 행 항목을 모두 채울 때까지 기다린 뒤 판정한다(시간 초과면 마지막 행).
    `connection == online`만 기다리면 이르다: c0의 g1은 폐쇄망 `apply`라 중앙에 게시 번들이 없어,
    outbox의 g1 성공 보고(첫 heartbeat 다음 seq)가 current 버전을 null로 덮고 다음 heartbeat가 다시
    채운다(control-api `reports.py`, VM run 20261108T205941Z)."""
    registered = cast(dict[str, object], setup["registered"])

    def judge(r: dict[str, object]) -> dict[str, bool]:
        return judges.judge_online_poll(
            r,
            ca_sha256=str(setup["ca_sha256"]),
            vm_ca_sha256=str(setup["vm_ca_sha256"]),
            site_id=registered.get("site_id"),
            want_site=ctx.site.site_id,
            pair=PAIR,
        )

    row, waited = buildhost.wait_fleet(
        ctx.site.site_id, lambda r: all(judge(r)[k] for k in ONLINE_POLL_ROW_CHECKS)
    )
    checks = judge(row)
    ctx.record(
        ITEM_ONLINE_POLL, all(checks.values()), checks=checks, waited_s=waited, row=row, **setup
    )


def step_online(ctx: Context) -> None:
    """c0(BASE g1, 폐쇄망) 위에서 중앙 c0로 온라인 경로 4항목. 앞 항목이 깨지면 뒤 항목은 실패로
    남기고(`online_guarded`) report로 넘어간다. 중앙 정리도 `online_guarded`가 한다
    (`--keep-central`이면 남긴다)."""
    site_id = ctx.site.site_id
    buildhost.sync_repo()  # 부분 재실행(`run.py online`)도 지금 트리의 central.py·c0 기록을 쓰게
    site.restore_c0(ctx)
    host = ctx.site.host()
    out = ctx.work / "online"
    status = buildhost.central("up", "--ca-out", golden.wsl_path(out / "central-root.crt"))
    ctx.central = status
    ctx.save()
    setup = site.site_online(
        ctx, host, out / "central-root.crt", str(status.get("ca_sha256") or "")
    )
    online_poll(ctx, setup)  # ① HTTPS heartbeat → /fleet online
    # ② 중앙이 게시한 BASE g2 → Updater가 polling으로 받아 적용
    pib = buildhost.build_site_bundle(
        ctx, ONLINE_GEN, BASE, datetime.now(UTC) - timedelta(minutes=5)
    )
    put = buildhost.central(
        "put", "--site", site_id, "--gen", str(ONLINE_GEN), "--pib", golden.wsl_path(pib)
    )
    published = judges.fleet_row(buildhost.central("fleet", "--site", site_id), site_id)
    snap, converge_s = site.converge(host, ONLINE_GEN)
    report = site.report_for(host, pib)
    attempt = judge_attempt(
        snap,
        report,
        expect="succeeded",
        want_digests=ctx.digests[BASE],
        want_applied=ONLINE_GEN,
        check_metric=True,
    )
    checks = {**judges.judge_online_published(put, published, PAIR), **attempt}
    ctx.record(
        ITEM_ONLINE_APPLY,
        all(checks.values()),
        checks=checks,
        put=put,
        desired=published.get("desired"),
        converge_s=round(converge_s, 1),
        code=report.get("code"),
        phase_durations_s=report.get("phase_durations_s"),
        logs=None if all(checks.values()) else report.get("logs"),
    )
    # ③ 온라인 보고 → /fleet current·last_attempt
    row, waited = buildhost.wait_fleet(
        site_id, lambda r: all(judges.judge_online_report(r, PAIR).values())
    )
    checks = judges.judge_online_report(row, PAIR)
    ctx.record(ITEM_ONLINE_REPORT, all(checks.values()), checks=checks, waited_s=waited, row=row)
    # ④ 중앙 중지 → 현장 그대로, stale → 재기동 뒤 online
    online_outage(ctx, host)


def online_guarded(ctx: Context, keep_central: bool) -> None:
    """`step_online`을 돌리되 예외가 `all`을 끊지 않게 한다 — 이번 호출에서 기록되지 않은 온라인
    항목은 실패(`error`)로 남기고 report로 넘어간다(`m5_guarded`와 같은 규칙). 중앙은
    `keep_central`이 아니면 내린다(실패해도)."""
    before = list(ctx.results)
    try:
        step_online(ctx)
    except Exception as exc:
        recorded = {r["name"] for r in ctx.results if not any(r is b for b in before)}
        for name in ONLINE_ITEMS:
            if name not in recorded:
                ctx.record(name, False, error=repr(exc)[-TAIL_CHARS:])
    finally:
        if not keep_central:
            try:
                buildhost.central("down")
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                log(f"online: central down failed: {exc}")


# ---------------------------------------------------------------- 보고


def step_report(ctx: Context) -> None:
    site.nft_mark(ctx, ctx.site.host())
    ctx.record(
        ITEM_NO_EXTERNAL,
        judges.no_external(ctx.nft_total),
        baseline=ctx.nft_before,
        total=ctx.nft_total,
        field_fwd=ctx.nft_total.get(golden.FIELD_RULE, 0),
        dns_lookups=ctx.dns_lookups,
    )
    ctx.out.mkdir(parents=True, exist_ok=True)
    metrics = ctx.out / "bundle-metrics.json"
    metrics.write_text(json.dumps(ctx.bundles, indent=2, ensure_ascii=False) + "\n", "utf-8")
    failed = [r["name"] for r in ctx.results if not r["ok"]]
    seen = {r["name"] for r in ctx.results}
    summary = {
        "run_id": ctx.run_id,
        "pair": f"{BASE}→{NEXT} (fault {FAULT})",
        "passed": len(ctx.results) - len(failed),
        "expected": len(expected_items()),
        "failed": failed,
        "missing": [name for name in expected_items() if name not in seen],
    }
    (ctx.out / "results.json").write_text(
        json.dumps({**summary, "results": ctx.results}, indent=2, ensure_ascii=False) + "\n",
        "utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m2d")
    parser.add_argument("command", choices=("all", "destroy", "central-down", *STEPS))
    parser.add_argument("--from", dest="start", choices=STEPS, default=None)
    parser.add_argument(
        "--keep-central",
        action="store_true",
        help="online 단계가 끝나도 중앙 c0를 내리지 않는다(15분 실측이 이어 쓴다)",
    )
    args = parser.parse_args(argv)
    if args.command == "destroy":
        hyperv.remove(SITE_A.vm_name, SITE_A.vm_dir / "disk.vhdx")
        return 0
    if args.command == "central-down":
        print(json.dumps(buildhost.central("down"), ensure_ascii=False))
        return 0
    runners: dict[str, Callable[[Context], None]] = {
        "prepare": step_prepare,
        "vm": step_vm,
        "install": step_install,
        "scenarios": step_scenarios,
        "crash": step_crash,
        "online": lambda ctx: online_guarded(ctx, args.keep_central),
        "report": step_report,
    }
    fresh = args.command == "prepare" or (args.command == "all" and args.start is None)
    if fresh:  # prepare는 새 실행(run id·결과)을 시작한다
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        ctx = Context(
            run_id=run_id,
            artifacts=str(paths.REPO / "artifacts" / "e2e" / f"m2d-{run_id}"),
            current=BASE,
            site=SITE_A,
            pair=PAIR,
        )
        ctx.save()
    else:
        ctx = Context.load(SITE_A, PAIR)
    steps = STEPS if args.command == "all" else (args.command,)
    if args.start is not None:
        steps = STEPS[STEPS.index(args.start) :]
    for step in steps:
        log(f"== {step}")
        runners[step](ctx)
    failed = [r for r in ctx.results if not r["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
