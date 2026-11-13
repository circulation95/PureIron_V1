"""G2-5 거부(M6-3b J1, site-b, c0 = NEXT g1) — 적용되면 안 되는 번들이 현장을 건드리지 않는지.

흐름(`rejections`, module fixture가 한 번 돈다): c0 복원 → 거부 재료(WSL `reject-materials`) →
c0에서 `E_SITE`(다른 현장 id) · `E_DIGEST`(payload 한 바이트) · `E_SIG`(root가 인증하지 않은
release key) · `E_SCHEMA`(NEXT 이미지 그대로에 스키마만 NEXT epoch + 2) · `E_UPDATER`(min_updater >
설치된 Updater) · 같은 gen 재빌드 no-op(exit 0, 거부 코드 없음) → `E_CERT` 재료(g3)를 **먼저** 만든
뒤 NEXT g2(NEXT 트리)를 적용(succeeded) → `E_CERT`(생성 시각 역행) · `E_GEN`(g2 뒤에 만든 g1).
번들은 스테이징 직전에 만든다 — 거부·no-op은 신뢰 상태(`max_seen_created_at`)를 바꾸지 않고, 커밋한
g2만 바꾼다.

거부마다 판정 `judges.judge_rejected`(종료 코드·거부 코드·core StartedAt·실행
이미지·edit-lock·applied gen). 검증 순서(서명·cert → site → gen → digest → 스키마·min_updater →
드래프트) 때문에 재료가 앞 단계에 걸리면 다른 코드가 나온다 — 그때는 재료를 고친다(판정을 느슨하게
하지 않는다). `E_DRAFT`는 G2-8(`draft.py`), `E_APPROVAL`은 M6-4.

설치 전 변조 2건(`preinstall`)은 site-b 부팅 때 이미 돌았다 — `check_preinstall`이 그 결과를
판정한다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "site"))

import tamper

from harness import buildhost, failures, g2, golden, judges, preinstall, scenarios, site, trees
from harness.context import TAIL_CHARS, Context, E2EError, InfraError
from harness.release_pair import ReleasePair
from harness.vm import Vm

REJECT_SITE = "site-b"
OTHER_SITE = "site-x"
"""`E_SITE` 번들의 현장 id — G2의 실제 현장(site-a·site-b)과 겹치지 않는다."""
REJECT_GEN = 2
"""c0(applied g1)에서 거부할 번들의 gen — 커밋 전 거부는 gen 상태를 바꾸지 않는다."""
NEXT_GEN = 2
CERT_GEN = 3
DIGEST_MEMBER = "images/core.tar*"
"""`E_DIGEST`에서 한 바이트를 바꾸는 payload 멤버(서명된 메타데이터는 그대로)."""
NEXT_APPLY = "next_g2"
"""NEXT g2(트리 변형) 적용 — `E_CERT`·`E_GEN`의 전제."""
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Case:
    key: str
    want_exit: int
    want_code: str | None


CASES: tuple[Case, ...] = (
    Case("E_SITE", 1, "E_SITE"),
    Case("E_DIGEST", 1, "E_DIGEST"),
    Case("E_SIG", 1, "E_SIG"),
    Case("E_SCHEMA", 1, "E_SCHEMA"),
    Case("E_UPDATER", 1, "E_UPDATER"),
    Case("same_gen", 0, None),
    Case("E_CERT", 1, "E_CERT"),
    Case("E_GEN", 1, "E_GEN"),
)
"""이 순서로 스테이징한다(앞 6건은 c0, 뒤 2건은 NEXT g2 뒤)."""
ITEMS = (*(case.key for case in CASES[:6]), NEXT_APPLY, *(case.key for case in CASES[6:]))
"""시험 `test_g2_5_rejected`의 매개변수(흐름 순서) — 거부 6건, NEXT g2 적용, 거부 2건."""


def item_name(key: str) -> str:
    return f"G2-5 {key}" if key == NEXT_APPLY else f"G2-5 reject {key}"


def above(version: str) -> str:
    """`version`(설치된 Updater, `MAJOR.MINOR.PATCH`)보다 높은 min_updater — 주 번호 + 1. 못 읽으면
    `E2EError`(제품: `pureiron-site --version`이 semver가 아니다)."""
    found = SEMVER.fullmatch(version.strip())
    if found is None:
        raise E2EError(f"installed updater version {version!r} is not MAJOR.MINOR.PATCH")
    return f"{int(found[1]) + 1}.0.0"


@dataclass(frozen=True)
class Variant:
    """거부 시험 번들 하나."""

    label: str
    gen: int
    product: str
    """달력 버전(`--product`)."""
    tree: Path
    site_id: str | None = None
    options: tuple[str, ...] = ()


Build = Callable[[Context, "Variant"], Path]


def build_variant(ctx: Context, variant: Variant) -> Path:
    return buildhost.build_variant(
        ctx,
        variant.gen,
        variant.product,
        datetime.now(UTC) - timedelta(minutes=5),
        golden.wsl_path(variant.tree),
        label=variant.label,
        site_id=variant.site_id,
        options=variant.options,
    )


def tamper_copy(pib: Path) -> Path:
    """`pib` 사본의 payload 한 멤버(`DIGEST_MEMBER`)에서 한 바이트를 바꾼다(`site/tamper.py`)."""
    copy = pib.with_name(f"{pib.stem}-tampered.pib")
    shutil.copyfile(pib, copy)
    tamper.flip(copy, DIGEST_MEMBER)
    return copy


def stage(host: Vm, pib: Path) -> subprocess.CompletedProcess[str]:
    return site.stage(host, pib, check=False, timeout_s=scenarios.STAGE_TIMEOUT_S)


def updater_version(host: Vm) -> str:
    return host.run("sudo pureiron-site --version").stdout.strip()


@dataclass(frozen=True)
class RejectRemote:
    """거부 시나리오의 원격·WSL 조작. 기본값이 실제 호출이다(단위 시험은 가짜)."""

    base: scenarios.Remote = field(default_factory=scenarios.Remote)
    materials: Callable[[ReleasePair], buildhost.RejectMaterials] = buildhost.reject_materials
    build: Build = build_variant
    tamper: Callable[[Path], Path] = tamper_copy
    stage: Callable[[Vm, Path], subprocess.CompletedProcess[str]] = stage
    updater_version: Callable[[Vm], str] = updater_version


def reject_once(
    run: g2.SiteRun, case: Case, pib: Path, remote: RejectRemote, *, want: str
) -> dict[str, bool]:
    """`pib`를 스테이징하고(적용되면 안 된다) 전·후 probe·보고로 판정해 기록한다. `want`는 지금 실행
    중이어야 하는 release."""
    ctx, host = run.ctx, run.ctx.site.host()
    before = remote.base.probe(host)
    result = remote.stage(host, pib)
    after = remote.base.probe(host)
    report = remote.base.report_for(host, pib)
    remote.base.drop(host, pib)
    checks = judges.judge_rejected(
        before,
        after,
        report,
        exit_code=result.returncode,
        want_exit=case.want_exit,
        want_code=case.want_code,
        want_digests=ctx.digests[want],
    )
    ok = all(checks.values())
    ctx.record(
        item_name(case.key),
        ok,
        bundle=pib.name,
        exit=result.returncode,
        code=report.get("code"),
        detail=report.get("detail"),
        checks=checks,
        stderr=None if ok else result.stderr[-TAIL_CHARS:],
        logs=None if ok else report.get("logs"),
    )
    return checks


def rejections(
    session: g2.Session, remote: RejectRemote | None = None
) -> dict[str, dict[str, bool]]:
    """site-b: c0 복원 → 거부 6건(c0) → NEXT g2 → 거부 2건. 항목(`ITEMS`) → 판정."""
    remote = remote or RejectRemote()
    pair = session.pair
    run, _ = scenarios.begin(session, REJECT_SITE, remote.base)
    ctx, host = run.ctx, run.ctx.site.host()
    mats = remote.materials(pair)
    version = pair.releases[pair.next]
    next_tree = session.tree_of(pair.next)
    min_updater = above(remote.updater_version(host))
    ctx.log(f"reject bundles (min_updater {min_updater}, schema epoch {mats.schema_epoch})")
    at_c0 = {
        "E_SITE": Variant("other-site", REJECT_GEN, version, next_tree, site_id=OTHER_SITE),
        "E_DIGEST": Variant("digest", REJECT_GEN, version, next_tree),
        "E_SIG": Variant("rogue-key", REJECT_GEN, version, next_tree, None, mats.signing_options()),
        "E_SCHEMA": Variant(
            "schema-jump", REJECT_GEN, mats.schema_version, next_tree, None, mats.schema_options()
        ),
        "E_UPDATER": Variant(
            "min-updater", REJECT_GEN, version, next_tree, None, ("--min-updater", min_updater)
        ),
        "same_gen": Variant("same-gen", 1, version, session.tree),
    }
    found: dict[str, dict[str, bool]] = {}
    cases = {case.key: case for case in CASES}
    for key, variant in at_c0.items():
        pib = remote.build(ctx, variant)
        if key == "E_DIGEST":
            pib = remote.tamper(pib)
        found[key] = reject_once(run, cases[key], pib, remote, want=pair.next)
    # E_CERT: g2보다 먼저 만든 번들 — g2를 커밋하면 max_seen_created_at이 그 뒤로 간다
    early = remote.build(ctx, Variant("before-g2", CERT_GEN, version, next_tree))
    found[NEXT_APPLY] = apply_next(session, run, remote)
    late = remote.build(ctx, Variant("after-g2", 1, version, session.tree))
    applied = {"next_g2_applied": all(found[NEXT_APPLY].values())}
    for key, pib in (("E_CERT", early), ("E_GEN", late)):
        found[key] = applied | reject_once(run, cases[key], pib, remote, want=pair.next)
    return found


def apply_next(session: g2.Session, run: g2.SiteRun, remote: RejectRemote) -> dict[str, bool]:
    """NEXT g2(NEXT 트리)를 적용한다 — succeeded·이미지 == NEXT·applied g2·트리 변형 적용."""
    pair, ctx = session.pair, run.ctx
    tree = session.tree_of(pair.next)
    done = scenarios.attempt(run, NEXT_GEN, pair.next, tree, remote.base)
    checks = judges.judge_attempt(
        done.snap,
        done.report,
        expect="succeeded",
        want_digests=ctx.digests[pair.next],
        want_applied=NEXT_GEN,
        check_metric=True,
    ) | judges.prefixed("next_tree_", judges.judge_tree(done.snap, trees.tree_expect(tree, pair)))
    ok = all(checks.values())
    ctx.record(
        item_name(NEXT_APPLY),
        ok,
        gen=NEXT_GEN,
        release=pair.next,
        result=judges.updater_state(done.snap).get("last_result"),
        code=done.report.get("code"),
        seconds=done.seconds,
        checks=checks,
        logs=None if ok else str(done.report.get("logs"))[-TAIL_CHARS:],
    )
    return checks


# ---------------------------------------------------------------- 설치 전 변조(site-b 부팅 때)

PREINSTALL_ITEM = "G2-5 tampered before install ({})"


def check_preinstall(session: g2.Session, name: str) -> dict[str, bool]:
    """site-b 부팅 때 돈 탐침 `name`(`preinstall.PROBES`)의 판정을 기록하고 돌려준다. 탐침 예외는
    분류를 유지한 새 예외로 다시 던진다(infra → `InfraError`, 그 밖 → `E2EError`). 탐침이 돌지
    않았고 현장이 부팅·설치에 실패했으면 그 예외를 다시 던진다 — 탐침 결과가 있으면 설치 실패와
    상관없이
    판정한다."""
    run = session.sites[preinstall.PREINSTALL_SITE]
    error = run.preinstall_errors.get(name)
    if error is not None:
        wrapped = InfraError if failures.classify(error) == failures.INFRA else E2EError
        raise wrapped(f"pre-install probe {name} failed: {error!r}") from error
    found = run.preinstall.get(name)
    if found is None:
        g2.site_up(session, preinstall.PREINSTALL_SITE)
        raise E2EError(f"pre-install probe {name} did not run")
    checks = preinstall.judge(name, found, run.ctx.runbook_bootstrap)
    run.ctx.record(PREINSTALL_ITEM.format(name), all(checks.values()), checks=checks, probe=found)
    return checks
