"""G2 시나리오 단위 시험이 같이 쓰는 가짜 세션(M6-3a) — VM·WSL 없이 `g2.Session`·`SiteRun`을 만든다.

시험 파일이 아니다(`test_*`가 아니라 수집되지 않는다). `conftest.py`가 이 디렉터리를 `sys.path`에
넣는다.
"""

from __future__ import annotations

import atexit
import dataclasses
import functools
import json
import shutil
import tempfile
from pathlib import Path

from harness import bundles, g2, judges, scenarios, trees
from harness.context import Context
from harness.judges import TreeExpect
from harness.release_pair import nightly
from harness.vm import Vm

SHA = "a" * 40
C0_GOOD: dict[str, object] = {"epoch": "5", "compressed_chunks": "2", "tag_history_rows": "6900"}
"""이력 시드 뒤 c0 기반 probe `db` — 압축 chunk·행 > 0, epoch = 쌍의 epoch 5."""


def session(tmp_path: Path) -> g2.Session:
    """nightly 쌍(BASE r5·NEXT n<sha7>, epoch 5) + 현장 둘. 카운터 기준은 c0 7·마지막 표시 9."""
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, 5)
    runs: dict[str, g2.SiteRun] = {}
    for base, _ in g2.SITE_ROLES:
        release = g2.site_release(pair, base.site_id)
        one = dataclasses.replace(base, state_file=tmp_path / base.site_id / "state.json")
        ctx = Context(
            run_id="20261110T180000Z",
            artifacts=str(tmp_path / base.site_id),
            current=release,
            site=one,
            pair=pair,
        )
        ctx.nft_total = dict.fromkeys(judges.AUDIT_COUNTERS, 0)
        ctx.nft_c0 = {"ext_out_v4": 7}
        ctx.nft_last = {"ext_out_v4": 9}
        runs[base.site_id] = g2.SiteRun(ctx, release, c0_at=0.0)
    return g2.Session("20261110T180000Z", SHA, pair, tmp_path, tmp_path / "sim.tar", runs)


EXPECTED_SCHEMA: dict[str, object] = {
    "epoch": "5",
    "tables": "alarm_journal,alarms,audit_log,devices,project_files,tag_history,tags,users",
    "columns": "devices.definition,devices.id,tags.definition,tags.path,users.name",
    "indexes": "audit_log_time_idx,sessions_user_id_idx",
    "caggs": "tag_history_1h,tag_history_1m",
    "realtime_caggs": "tag_history_1h,tag_history_1m",
}
"""가짜 기대 스키마(D9) — 가짜 스냅샷 `db`의 기본 이름 집합과 같다."""
ROWS: dict[str, object] = {
    "users_rows": "2",
    "audit_log_rows": "5",
    "alarm_journal_rows": "0",
    "tag_history_rows": "6900",
}


def digests(release: str) -> dict[str, str]:
    """release마다 다른 가짜 config digest(site-db는 모두 같다)."""
    return {
        "core": f"sha256:core-{release}",
        "collector": f"sha256:collector-{release}",
        "web": f"sha256:web-{release}",
        "site-db": "sha256:site-db",
    }


@functools.cache
def shared_trees() -> tuple[Path, dict[str, Path]]:
    """G2 트리와 NEXT·FAULT 트리(`session`의 쌍) — 시험 프로세스에서 한 번만 만든다(읽기 전용)."""
    root = Path(tempfile.mkdtemp(prefix="g2-trees-"))
    atexit.register(shutil.rmtree, root, True)
    tree = trees.g2_tree(root / "tree")
    p = session(root).pair
    return tree, {
        name: bundles.release_tree(root / "trees" / name, name, p, source=tree)
        for name in (p.next, p.fault, p.fault_migrate)
    }


@functools.cache
def tree_expect(tree: Path) -> TreeExpect:
    return trees.tree_expect(tree, session(tree.parent).pair)


def scenario_session(tmp_path: Path) -> g2.Session:
    """`session` + 실제 G2 트리·NEXT/FAULT 트리(`bundles.release_tree`, 공용 사본)·release
    digest·기대 스키마."""
    s = session(tmp_path)
    s.tree, release_trees = shared_trees()
    p = s.pair
    s.release_trees = dict(release_trees)
    s.expected_schema = dict(EXPECTED_SCHEMA)
    for run in s.sites.values():
        run.ctx.digests = {
            name: digests(name) for name in (p.base, p.next, p.fault, p.fault_migrate)
        }
    return s


def snap(
    tree: TreeExpect,
    release: str,
    *,
    applied: int,
    attempted: int,
    result: str,
    db: dict[str, object] | None = None,
    revs: tuple[int, int] = (0, 0),
    **changes: object,
) -> dict[str, object]:
    """probe 스냅샷 가짜 — `tree`가 적용됐고 `release` 이미지가 돌며 ready·edit-lock 해제·경보 없음.
    `db`는 기본 db 위에 덮고, `changes`는 최상위 키를 바꾼다."""
    base_db: dict[str, object] = {
        **EXPECTED_SCHEMA,
        **ROWS,
        "compressed_chunks": "2",
        "project_files": str(tree.files),
        "device_ids": ",".join(sorted(tree.devices)),
        "tag_paths": ",".join(sorted(tree.tags)),
        "alarm_ids": ",".join(sorted(tree.alarms)),
        "tag_defs": json.dumps({tree.tree_tag: {"description": tree.tree_tag_description}}),
    }
    out: dict[str, object] = {
        "updater": {
            "applied_gen": applied,
            "last_attempted_gen": attempted,
            "last_result": result,
            "attempt": None,
            "alarm": None,
        },
        "containers": {
            svc: {"image_id": d, "running": True} for svc, d in digests(release).items()
        },
        "readyz": {"status": 200},
        "project": {
            "status": 200,
            "body": {
                "edit_locked": False,
                "version": "v1",
                "content_hash": tree.content_hash,
                "site_rev": revs[0],
                "base_site_rev": revs[1],
            },
        },
        "db": {**base_db, **(db or {})},
        "metrics": {"updater_failed_manual": 0.0},
    }
    return {**out, **changes}


class FakeRemote:
    """시나리오의 원격 조작 가짜 — gen마다 정한 (수렴 스냅샷, 보고, 초)를 내고 호출을 `events`에
    남긴다. `remote()`가 `scenarios.Remote`를 만든다."""

    def __init__(
        self,
        before: dict[str, object],
        attempts: dict[int, tuple[dict[str, object], dict[str, object], float]],
    ) -> None:
        self.before = before
        self.attempts = attempts
        self.events: list[str] = []

    def remote(self) -> scenarios.Remote:
        def restore(run: g2.SiteRun) -> None:
            self.events.append(f"restore {run.ctx.site.site_id}")

        def ready(host: Vm) -> dict[str, object]:
            self.events.append("ready")
            return self.before

        def build(ctx: Context, gen: int, release: str, tree: Path) -> Path:
            self.events.append(f"build g{gen} {release} {tree.name}")
            return ctx.out / f"g{gen}.pib"

        def stage(host: Vm, pib: Path) -> None:
            self.events.append(f"stage {pib.name}")

        def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
            return self.attempts[gen][0], self.attempts[gen][2]

        def report_for(host: Vm, pib: Path) -> dict[str, object]:
            return self.attempts[int(pib.stem.removeprefix("g"))][1]

        return scenarios.Remote(
            restore=restore,
            wait_ready=ready,
            build=build,
            stage=stage,
            converge=converge,
            report_for=report_for,
            drop=lambda host, pib: None,
        )
