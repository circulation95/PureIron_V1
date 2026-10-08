"""compose 수직 경로: site-db → migrate up → core·collector·web → readyz 200 (US-005).

M3a-2: 시뮬레이터 100태그 apply → 10s 내 100% GOOD, collector kill → 태그 BAD·장비 DISCONNECTED.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

import asyncpg
import pytest

from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database
from pureiron_core.project.ports import EditLockConflictError
from pureiron_core.project.stub import PgProjectStateStore

from .conftest import (
    CORE_PORT,
    MIGRATIONS,
    WEB_PORT,
    SiteDb,
    Stack,
    compose,
    core_cli,
    holding_tag,
    modbus_device,
    wait_http,
    write_project,
)

pytestmark = pytest.mark.integration

CORE = f"http://127.0.0.1:{CORE_PORT}/api/v1"
WEB = f"http://127.0.0.1:{WEB_PORT}"


def _request(method: str, url: str, token: str, body: dict[str, object] | None = None) -> int:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        status: int = response.status
        return status


@pytest.mark.compose
def test_readyz_200_through_core_and_web(stack: Stack) -> None:
    ready = json.loads(wait_http(f"{CORE}/readyz", timeout_s=120))
    assert ready["status"] == "ready"
    assert ready["checks"]["db"]["ok"] is True
    assert (
        ready["checks"]["schema_envelope"]["cur"] == migrate.load_catalog(MIGRATIONS).app_expected
    )
    assert ready["checks"]["collector"]["ok"] is True, "collector must hold a gRPC session"

    via_web = json.loads(wait_http(f"{WEB}/api/v1/readyz", timeout_s=60))
    assert via_web["status"] == "ready"
    index = wait_http(f"{WEB}/", timeout_s=30).decode()
    assert '<div id="root">' in index


@pytest.mark.compose
def test_edit_lock_with_updater_secret(stack: Stack) -> None:
    wait_http(f"{CORE}/readyz", timeout_s=120)
    lock = f"{CORE}/project/edit-lock"
    assert _request("POST", lock, stack.updater_token, {"owner": "updater", "generation": 1}) == 200
    project = json.loads(wait_http(f"{CORE}/project"))
    assert project["edit_locked"] is True
    assert _request("DELETE", f"{lock}?owner=updater", stack.updater_token) == 204


async def test_edit_lock_race_has_single_winner(site_db: SiteDb) -> None:
    db = Database(site_db.dsn, password=site_db.password)
    store = PgProjectStateStore(db)
    try:
        await store.release_edit_lock("a")
        await store.release_edit_lock("b")
        results = await asyncio.gather(
            store.acquire_edit_lock("a", 1),
            store.acquire_edit_lock("b", 1),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, int)]
        losers = [r for r in results if isinstance(r, EditLockConflictError)]
        assert len(winners) == 1
        assert len(losers) == 1
        owner = (await store.get()).edit_lock_owner
        assert owner in {"a", "b"}
        assert await store.acquire_edit_lock(owner, 1) == winners[0]  # 같은 owner·gen 재요청은 멱등
        await store.release_edit_lock(owner)
        assert (await store.get()).edit_lock_owner is None
    finally:
        await db.close()


@pytest.mark.fresh_db
async def test_apply_records_base_site_rev_and_restore_roundtrip(site_db: SiteDb) -> None:
    db = Database(site_db.dsn, password=site_db.password)
    store = PgProjectStateStore(db)
    try:
        conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
        try:
            await conn.execute("UPDATE project_state SET site_rev = 5")
        finally:
            await conn.close()
        applied = await store.apply(project_id="p", version="v1", content_hash="sha256:" + "0" * 64)
        assert (applied.site_rev, applied.base_site_rev) == (5, 5)
        assert applied.applied_at is not None
        snapshot = applied.to_snapshot()

        await store.apply(project_id="p", version="v2", content_hash="sha256:" + "1" * 64)
        restored = await store.restore(snapshot)
        assert restored.version == "v1"
        assert restored.to_snapshot() == snapshot
    finally:
        await db.close()


SIM_TAGS = 100
GOOD_WITHIN_S = 10.0


def _readyz() -> dict[str, object]:
    """준비 여부와 무관하게(503 포함) readyz 본문."""
    try:
        with urllib.request.urlopen(f"{CORE}/readyz", timeout=3) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    data: dict[str, object] = json.loads(body)
    return data


def _wait_acquisition(
    predicate: Callable[[dict[str, object]], bool], timeout_s: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        acquisition = _readyz().get("acquisition")
        if isinstance(acquisition, dict):
            last = cast("dict[str, object]", acquisition)
            if predicate(last):
                return last
        time.sleep(0.25)
    raise AssertionError(f"acquisition did not reach the expected state in {timeout_s}s: {last}")


@pytest.mark.compose
def test_sim_100_tags_good_then_collector_kill(stack: Stack, tmp_path: Path) -> None:
    wait_http(f"{CORE}/readyz", timeout_s=120)
    root = write_project(
        tmp_path / "sim-project",
        {"sim": modbus_device("sim", "simulator", 5020)},
        {"sim": [holding_tag(f"sim/r{i:03d}", "sim", i) for i in range(SIM_TAGS)]},
    )
    applied = core_cli(stack.cli_env("pureiron"), "apply-definitions", str(root), "--json")
    assert applied.returncode == 0, applied.stdout + applied.stderr

    good = _wait_acquisition(
        lambda a: a.get("tags_total") == SIM_TAGS and a.get("tags_good") == SIM_TAGS,
        GOOD_WITHIN_S,
    )
    assert good["good_ratio"] == 1.0
    _wait_acquisition(lambda a: a.get("devices_connected") == 1, 10)

    try:
        compose(stack.compose_env, "kill", "collector")
        down = _wait_acquisition(
            lambda a: a.get("tags_good") == 0 and a.get("devices_connected") == 0, 15
        )
        assert down["tags_total"] == SIM_TAGS
        assert down["good_ratio"] == 0.0
    finally:
        compose(stack.compose_env, "up", "-d", "collector")
        wait_http(f"{CORE}/readyz", timeout_s=60)  # 다른 compose 테스트를 위해 세션 복구
