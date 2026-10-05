"""compose 수직 경로: site-db → migrate up → core·collector·web → readyz 200 (US-005)."""

from __future__ import annotations

import asyncio
import json
import urllib.request

import asyncpg
import pytest

from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database
from pureiron_core.project.ports import EditLockConflictError
from pureiron_core.project.stub import PgProjectStateStore

from .conftest import CORE_PORT, MIGRATIONS, WEB_PORT, SiteDb, Stack, wait_http

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
