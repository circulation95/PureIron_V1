"""G2-8 드래프트 회수(PR 전, M6-3b J2, site-b) — 현장 PATCH가 흡수되기 전에는 번들이 막히고, 흡수한
트리로만 다시 적용되며, 그 뒤의 롤백이 드래프트 기준점을 되돌리지 않는지.

흐름(`draft_recall`, module fixture가 한 번 돈다): c0 복원 → admin 로그인 → 운영 값 PATCH 3회
(site_rev +3) → 미흡수 repo(G2 트리 그대로 — `absorbs`에 site-a 키만 있다)로 만든 번들 → `E_DRAFT`
→ `pureiron-site project export` → 호스트 → WSL `absorb`·`gate` → 흡수한 트리 번들 적용(succeeded)
→ FAULT-h(리뷰 L1: site_rev·base_site_rev가 0이 아닌 상태의 롤백).

G2 트리를 미흡수 repo로 쓰는 이유(3a 보고서 8절): 게이트가 다른 현장의 키(site-a: 3)를 잘못 읽는
결함이면 site_rev 3 == 3으로 통과해 `E_DRAFT`가 나오지 않는다 — PATCH를 정확히 3회 하는 것과 함께 그
결함도 잡는다. 판정은 `judges.judge_draft_patch`·`judge_rejected`·`judge_absorb`·
`judge_draft_reapplied`·`judge_draft_rollback`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harness import buildhost, g2, golden, judges, rejects, scenarios, site
from harness.context import TAIL_CHARS, Context
from harness.vm import Vm

DRAFT_SITE = "site-b"
DRAFT_GEN = 2
"""미흡수 repo 번들(`E_DRAFT`) — 커밋 전 거부라 gen 상태를 바꾸지 않는다."""
ABSORB_GEN = 3
FAULT_GEN = 4
PATCH, DRAFT_GATE, ABSORB, REAPPLY, ROLLBACK = (
    "patch",
    "draft_gate",
    "absorb",
    "reapply",
    "rollback_keeps_drafts",
)
ITEMS = (PATCH, DRAFT_GATE, ABSORB, REAPPLY, ROLLBACK)
"""시험 `test_g2_8_draft_recall`의 매개변수(흐름 순서)."""
ITEM_NAMES = {
    PATCH: "G2-8 site PATCH x3 via web (site_rev +3)",
    DRAFT_GATE: "G2-8 reject unabsorbed drafts (E_DRAFT)",
    ABSORB: "G2-8 export -> absorb -> gate",
    REAPPLY: "G2-8 reapply absorbed tree (base_site_rev == site_rev, PATCH kept)",
    ROLLBACK: "G2-8 FAULT-h rollback keeps site_rev, base_site_rev, PATCH and tree",
}


def site_patch(host: Vm) -> tuple[dict[str, object], dict[str, object]]:
    return site.site_patch(host, alarm2=(judges.DRAFT_ALARM, judges.DRAFT_ALARM_BODY))


def export_draft(ctx: Context, host: Vm) -> tuple[dict[str, object], Path | None, Path | None]:
    """`pureiron-site project export` → 드래프트·현장 공개키(enrollment)를 호스트로. (기록,
    드래프트,
    enrollment) — export가 실패하면 파일은 None."""
    done = host.run(
        site.as_root(
            f"pureiron-site project export --out {site.DRAFT_REMOTE} && "
            f"chown pureiron: {site.DRAFT_REMOTE} && "
            f"install -o pureiron -m 0644 {site.STATE_DIR}/enrollment.json "
            f"{site.ENROLLMENT_REMOTE}"
        ),
        check=False,
    )
    note: dict[str, object] = {
        "rc": done.returncode,
        "stdout": done.stdout[-TAIL_CHARS:],
        "stderr": done.stderr[-TAIL_CHARS:],
    }
    if done.returncode != 0:
        return note, None, None
    local = ctx.work / "draft"
    draft, enrollment = local / "draft.tar", local / "enrollment.json"
    host.get(site.DRAFT_REMOTE, draft)
    host.get(site.ENROLLMENT_REMOTE, enrollment)
    return note, draft, enrollment


def absorb(ctx: Context, draft: Path, enrollment: Path, repo: Path) -> dict[str, object]:
    """WSL `bundles.py absorb`: `repo`(G2 트리) 사본(`ctx.work/draft/absorbed`)에 드래프트를
    회수하고
    게이트를 사전 점검한다. 결과 JSON(`dir`은 WSL 경로)."""
    dest = ctx.work / "draft" / "absorbed"
    raw = buildhost.wsl(
        f"{buildhost.bundles_py('absorb', buildhost.NIGHTLY_WORK)} "
        f"--draft '{golden.wsl_path(draft)}' --site-key '{golden.wsl_path(enrollment)}' "
        f"--site {ctx.site.site_id} --repo '{golden.wsl_path(repo)}' "
        f"--dest '{golden.wsl_path(dest)}'"
    )
    found = cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    return {**found, "host_dir": str(dest)}


@dataclass(frozen=True)
class DraftRemote:
    """드래프트 시나리오의 원격·WSL 조작. 기본값이 실제 호출이다(단위 시험은 가짜)."""

    base: scenarios.Remote = field(default_factory=scenarios.Remote)
    patch: Callable[[Vm], tuple[dict[str, object], dict[str, object]]] = site_patch
    stage: Callable[[Vm, Path], subprocess.CompletedProcess[str]] = rejects.stage
    export: Callable[[Context, Vm], tuple[dict[str, object], Path | None, Path | None]] = (
        export_draft
    )
    absorb: Callable[[Context, Path, Path, Path], dict[str, object]] = absorb


def draft_recall(
    session: g2.Session, remote: DraftRemote | None = None
) -> dict[str, dict[str, bool]]:
    """site-b: c0 → PATCH 3회 → `E_DRAFT` → export·absorb·gate → 흡수 트리 재적용 → FAULT-h 롤백.
    항목(`ITEMS`) → 판정. 앞 단계가 실패하면 뒤 항목은 그 단계 실패(`after_<항목>` 거짓)로
    남긴다."""
    remote = remote or DraftRemote()
    pair = session.pair
    run, c0 = scenarios.begin(session, DRAFT_SITE, remote.base)
    ctx, host = run.ctx, run.ctx.site.host()
    rev0 = judges.revs(judges.project_body(c0))
    want_rev = (rev0[0] if rev0 is not None else -1) + judges.DRAFT_PATCHES
    found: dict[str, dict[str, bool]] = {}
    out, admin = remote.patch(host)
    after = remote.base.probe(host)
    found[PATCH] = {"admin_created": admin.get("rc") == 0} | judges.judge_draft_patch(
        out, judges.db_of(after)
    )
    ctx.record(
        ITEM_NAMES[PATCH],
        all(found[PATCH].values()),
        checks=found[PATCH],
        admin=admin,
        replies=out,
        want_rev=want_rev,
    )
    unabsorbed = remote.base.build(ctx, DRAFT_GEN, pair.next, session.tree)
    found[DRAFT_GATE] = rejects.reject_once(
        run,
        rejects.Case("E_DRAFT", 1, "E_DRAFT"),
        unabsorbed,
        rejects.RejectRemote(base=remote.base, stage=remote.stage),
        want=pair.next,
        name=ITEM_NAMES[DRAFT_GATE],
    )
    note, draft_tar, enrollment = remote.export(ctx, host)
    result: dict[str, object] = {}
    if draft_tar is not None and enrollment is not None:
        result = remote.absorb(ctx, draft_tar, enrollment, session.tree)
    found[ABSORB] = {"export": draft_tar is not None} | judges.judge_absorb(
        result, want_rev, DRAFT_SITE
    )
    ctx.record(
        ITEM_NAMES[ABSORB], all(found[ABSORB].values()), checks=found[ABSORB], export=note, **result
    )
    if not all(found[ABSORB].values()):
        found[REAPPLY] = {"after_absorb": False}
        found[ROLLBACK] = {"after_absorb": False}
        return found
    content_hash = judges.absorbed_hash(result)
    absorbed = Path(str(result["host_dir"]))
    found[REAPPLY] = reapply(
        session, run, absorbed, want_rev=want_rev, hashed=content_hash, remote=remote
    )
    found[ROLLBACK] = {"after_reapply": all(found[REAPPLY].values())} | rollback(
        session, run, want_rev=want_rev, hashed=content_hash, remote=remote
    )
    return found


def reapply(
    session: g2.Session,
    run: g2.SiteRun,
    tree: Path,
    *,
    want_rev: int,
    hashed: str | None,
    remote: DraftRemote,
) -> dict[str, bool]:
    """흡수한 트리(`tree`) NEXT g`ABSORB_GEN` — succeeded·이미지 == NEXT·applied gen,
    `base_site_rev == site_rev == want_rev`·PATCH 값 유지·content_hash == absorb 출력."""
    pair, ctx = session.pair, run.ctx
    done = scenarios.attempt(run, ABSORB_GEN, pair.next, tree, remote.base)
    checks = judges.judge_attempt(
        done.snap,
        done.report,
        expect="succeeded",
        want_digests=ctx.digests[pair.next],
        want_applied=ABSORB_GEN,
        check_metric=True,
    ) | judges.judge_draft_reapplied(done.snap, want_rev, hashed)
    record(run, REAPPLY, checks, done)
    return checks


def rollback(
    session: g2.Session,
    run: g2.SiteRun,
    *,
    want_rev: int,
    hashed: str | None,
    remote: DraftRemote,
) -> dict[str, bool]:
    """FAULT-h g`FAULT_GEN`(FAULT 트리) → 롤백 뒤 드래프트 기준점·PATCH·흡수 트리 유지."""
    pair, ctx = session.pair, run.ctx
    before = remote.base.probe(ctx.site.host())
    done = scenarios.attempt(run, FAULT_GEN, pair.fault, session.tree_of(pair.fault), remote.base)
    checks = judges.judge_draft_rollback(
        before,
        done.snap,
        done.report,
        want_digests=ctx.digests[pair.next],
        want_rev=want_rev,
        content_hash=hashed,
    )
    record(run, ROLLBACK, checks, done)
    return checks


def record(run: g2.SiteRun, item: str, checks: dict[str, bool], done: scenarios.Attempt) -> None:
    ok = all(checks.values())
    run.ctx.record(
        ITEM_NAMES[item],
        ok,
        gen=done.gen,
        release=done.release,
        result=judges.updater_state(done.snap).get("last_result"),
        code=done.report.get("code"),
        seconds=done.seconds,
        checks=checks,
        revs=judges.revs(judges.project_body(done.snap)),
        logs=None if ok else str(done.report.get("logs"))[-TAIL_CHARS:],
    )
