"""G2-8 드래프트 회수(M6-3b J2) — 흐름(c0 → PATCH 3회 → 미흡수 G2 트리 번들 E_DRAFT → export·absorb
→ 흡수 트리 재적용 → FAULT-h 롤백)과 판정(site_rev +3·absorbs[site-b]·base_site_rev == site_rev·
롤백 뒤 드래프트 기준점 유지)을 VM·WSL 없이 고정한다."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import site_api
from g2_fakes import scenario_session, snap, tree_expect
from harness import draft, g2, judges, scenarios
from harness.context import Context
from harness.judges import TreeExpect
from harness.vm import Vm

HASH = "sha256:" + "d" * 64
PATCHED_DB: dict[str, object] = {
    "tag_defs": json.dumps({judges.PATCH_TAG: judges.PATCH_TAG_BODY}),
    "alarm_defs": json.dumps(
        {judges.PATCH_ALARM: judges.PATCH_ALARM_BODY, judges.DRAFT_ALARM: judges.DRAFT_ALARM_BODY}
    ),
}


def patch_out(steps: int = 3) -> dict[str, object]:
    """site_api 출력 — c0 site_rev 0에서 PATCH `steps`회."""
    out: dict[str, object] = {
        "project_before": [200, {"site_rev": 0, "base_site_rev": 0}],
        "login": [200, {"user": {}}],
        "session_cookie": True,
        "project_after": [200, {"site_rev": steps, "base_site_rev": 0}],
    }
    for step, key in enumerate(("tag", "alarm", "alarm2")[:steps], start=1):
        out[key] = [200, {"site_rev": step}]
    return out


def judges_containers(s: dict[str, object]) -> dict[str, object]:
    value = s.get("containers")
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


class DraftFake:
    """site-b c0(NEXT g1, site_rev 0)의 드래프트 시나리오 원격 가짜. 바꿀 수 있는 것: PATCH 수,
    absorb 출력, 재적용·롤백 뒤 revs, 미흡수 번들의 결과(rc·코드)."""

    def __init__(self, session: g2.Session) -> None:
        self.session = session
        pair = session.pair
        self.absorbed: TreeExpect = dataclasses.replace(
            tree_expect(session.tree), content_hash=HASH
        )
        c0 = snap(tree_expect(session.tree), pair.next, applied=1, attempted=1, result="succeeded")
        core = {"image_id": f"sha256:core-{pair.next}", "running": True, "started_at": "t0"}
        self.current = {**c0, "containers": {**judges_containers(c0), "core": core}}
        self.events: list[str] = []
        self.steps = 3
        self.absorb_out = f"absorbs[site-b]: None -> 3\nok: {HASH}\n"
        self.reapplied_revs = (3, 3)
        self.rolled_revs = (3, 3)
        self.gate = (1, "E_DRAFT")
        self.reports: dict[str, dict[str, object]] = {}

    def attempt_snap(self, gen: int) -> tuple[dict[str, object], dict[str, object]]:
        pair = self.session.pair
        if gen == draft.ABSORB_GEN:
            done = snap(
                self.absorbed,
                pair.next,
                applied=3,
                attempted=3,
                result="succeeded",
                db=PATCHED_DB,
                revs=self.reapplied_revs,
            )
            return done, {"code": None, "failed_manual": False}
        done = snap(
            self.absorbed,
            pair.next,
            applied=3,
            attempted=4,
            result="failed",
            db=PATCHED_DB,
            revs=self.rolled_revs,
        )
        return done, {"code": "E_HEALTH", "failed_manual": False}

    def remote(self) -> draft.DraftRemote:
        def build(ctx: Context, gen: int, release: str, tree: Path) -> Path:
            self.events.append(f"build g{gen} {release} {tree.name}")
            return ctx.out / f"g{gen}.pib"

        def stage_attempt(host: Vm, pib: Path) -> None:
            self.events.append(f"stage {pib.name}")
            self.current, self.reports[pib.name] = self.attempt_snap(int(pib.stem[1:]))

        def stage_reject(host: Vm, pib: Path) -> subprocess.CompletedProcess[str]:
            self.events.append(f"stage {pib.name} (unabsorbed)")
            self.reports[pib.name] = {"code": self.gate[1]}
            return subprocess.CompletedProcess([], self.gate[0], "", "")

        def patch(host: Vm) -> tuple[dict[str, object], dict[str, object]]:
            self.events.append("patch")
            self.current = {
                **self.current,
                "db": {**judges.db_of(self.current), **PATCHED_DB},
                "project": {
                    "status": 200,
                    "body": {
                        **judges.project_body(self.current),
                        "site_rev": self.steps,
                        "base_site_rev": 0,
                    },
                },
            }
            return patch_out(self.steps), {"rc": 0}

        def export(ctx: Context, host: Vm) -> tuple[dict[str, object], Path | None, Path | None]:
            self.events.append("export")
            return {"rc": 0}, ctx.work / "draft.tar", ctx.work / "enrollment.json"

        def absorb(ctx: Context, tar: Path, enrollment: Path, repo: Path) -> dict[str, object]:
            self.events.append(f"absorb {repo.name}")
            return {
                "absorb": {"rc": 0, "stdout": self.absorb_out},
                "gate": {"rc": 0},
                "host_dir": str(ctx.work / "absorbed"),
            }

        base = scenarios.Remote(
            restore=lambda run: self.events.append(f"restore {run.ctx.site.site_id}"),
            probe=lambda host: self.current,
            wait_ready=lambda host: self.current,
            build=build,
            stage=stage_attempt,
            converge=lambda host, gen: (self.current, 1.5),
            report_for=lambda host, pib: self.reports.get(pib.name, {}),
            drop=lambda host, pib: None,
            clock=lambda: 0.0,
        )
        return draft.DraftRemote(
            base=base, patch=patch, stage=stage_reject, export=export, absorb=absorb
        )


def run(tmp_path: Path, **changes: object) -> tuple[dict[str, dict[str, bool]], DraftFake]:
    session = scenario_session(tmp_path)
    fake = DraftFake(session)
    for key, value in changes.items():
        setattr(fake, key, value)
    return draft.draft_recall(session, fake.remote()), fake


def failures(found: dict[str, dict[str, bool]]) -> dict[str, list[str]]:
    return {item: scenarios.failed(checks) for item, checks in found.items()}


def test_draft_recall_flow_and_every_item_passes(tmp_path: Path) -> None:
    found, fake = run(tmp_path)
    pair = fake.session.pair
    assert fake.events == [
        "restore site-b",
        "patch",
        f"build g2 {pair.next} tree",  # 미흡수 repo = G2 트리 그대로(absorbs: site-a만)
        "stage g2.pib (unabsorbed)",
        "export",
        "absorb tree",
        f"build g3 {pair.next} absorbed",
        "stage g3.pib",
        f"build g4 {pair.fault} {pair.fault}",
        "stage g4.pib",
    ]
    assert list(found) == list(draft.ITEMS)
    assert failures(found) == {item: [] for item in draft.ITEMS}
    saved = json.loads(fake.session.sites["site-b"].ctx.site.state_file.read_text("utf-8"))
    assert [r["name"] for r in saved["results"]] == [draft.ITEM_NAMES[i] for i in draft.ITEMS]


def test_two_patches_are_not_enough(tmp_path: Path) -> None:
    """변이: PATCH 2회여도 통과하면 게이트가 site-a 키(3)를 잘못 읽는 결함의 함정이 없어진다."""
    found, _ = run(tmp_path, steps=2)
    assert failures(found)[draft.PATCH] == ["alarm2_patch", "site_rev"]


def test_the_unabsorbed_bundle_must_be_rejected_with_e_draft(tmp_path: Path) -> None:
    found, _ = run(tmp_path, gate=(0, None))
    assert failures(found)[draft.DRAFT_GATE] == ["code", "exit"]


def test_absorb_must_record_the_site_b_revision(tmp_path: Path) -> None:
    """변이: absorbs[site-b] 확인을 빼면 site-a 키만 있는 흡수가 통과한다."""
    found, _ = run(tmp_path, absorb_out=f"absorbs[site-a]: 3 -> 3\nok: {HASH}\n")
    assert failures(found)[draft.ABSORB] == ["absorbs_rev"]
    assert failures(found)[draft.REAPPLY] == ["after_absorb"]


def test_reapply_needs_base_site_rev_equal_to_site_rev(tmp_path: Path) -> None:
    found, _ = run(tmp_path, reapplied_revs=(3, 0))
    assert failures(found)[draft.REAPPLY] == ["base_site_rev"]


def test_rollback_after_drafts_must_keep_site_rev_and_base_site_rev(tmp_path: Path) -> None:
    """리뷰 L1·변이: 롤백이 드래프트 기준점을 0으로 되돌리면 잡는다."""
    found, _ = run(tmp_path, rolled_revs=(0, 0))
    assert failures(found)[draft.ROLLBACK] == ["revs_kept"]


@pytest.mark.parametrize("key", ["project_before", "project_after"])
def test_patch_judge_with_unreadable_revisions_is_false(key: str) -> None:
    out = patch_out()
    out[key] = [500, "error"]
    checks = judges.judge_draft_patch(out, {**PATCHED_DB})
    assert not checks["site_rev"]


def site_api_args(**extra: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        username="e2e-admin",
        password_file=extra.pop("password_file"),
        tag="line1/temp",
        tag_body="{}",
        alarm="temp-high",
        alarm_body="{}",
        alarm2=extra.get("alarm2"),
        alarm2_body=extra.get("alarm2_body") or "{}",
    )


def test_site_api_patches_a_second_alarm_only_when_asked(tmp_path: Path) -> None:
    password = tmp_path / "pw"
    password.write_text("secret\n", encoding="utf-8")
    calls: list[str] = []

    def call(
        method: str, path: str, body: object | None = None, cookie: str | None = None
    ) -> tuple[int, Any, list[str]]:
        calls.append(f"{method} {path}")
        cookies = ["pureiron_session=tok; Path=/"] if path.endswith("/login") else []
        return 200, {"site_rev": len(calls)}, cookies

    out = site_api.patch(site_api_args(password_file=str(password)), call)
    assert "alarm2" not in out
    assert "PATCH /api/v1/project/alarms/temp-low" not in calls
    calls.clear()
    out = site_api.patch(
        site_api_args(
            password_file=str(password), alarm2="temp-low", alarm2_body='{"setpoint": 6.5}'
        ),
        call,
    )
    assert "PATCH /api/v1/project/alarms/temp-low" in calls
    assert out["alarm2"][0] == 200
