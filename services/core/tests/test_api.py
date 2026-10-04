from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core import __version__
from pureiron_core.api.health import build_health_router
from pureiron_core.api.project import build_project_router
from pureiron_core.platform.db import DatabaseUnavailableError
from pureiron_core.platform.migrate import Envelope
from pureiron_core.project.ports import EditLockConflictError, ProjectState

TOKEN = "s3cret-updater-token"


@dataclass
class FakeDb:
    up: bool = True

    async def ping(self) -> bool:
        return self.up


@dataclass
class FakeSchema:
    cur: int | None = 1
    app_expected: int = 1
    app_max_compatible: int = 2

    async def envelope(self) -> Envelope:
        return Envelope(self.cur, self.app_expected, self.app_max_compatible)


@dataclass
class FakeCollector:
    collector_connected: bool = True


def _health_client(db: FakeDb, schema: FakeSchema, collector: FakeCollector) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_health_router(db=db, schema=schema, collector=collector), prefix="/api/v1"
    )
    return TestClient(app)


def test_healthz() -> None:
    client = _health_client(FakeDb(), FakeSchema(), FakeCollector())
    assert client.get("/api/v1/healthz").json() == {"status": "ok", "version": __version__}


def test_readyz_ready() -> None:
    response = _health_client(FakeDb(), FakeSchema(cur=2), FakeCollector()).get("/api/v1/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["schema_envelope"] == {
        "ok": True,
        "detail": None,
        "cur": 2,
        "app_expected": 1,
        "app_max_compatible": 2,
    }


def test_readyz_db_down() -> None:
    response = _health_client(FakeDb(up=False), FakeSchema(), FakeCollector()).get("/api/v1/readyz")
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["db"] == {"ok": False, "detail": "site-db unavailable"}
    assert checks["schema_envelope"]["ok"] is False
    assert checks["schema_envelope"]["cur"] is None
    assert checks["schema_envelope"]["app_expected"] == 1


@pytest.mark.parametrize(
    ("cur", "fragment"), [(None, "not migrated"), (3, "outside"), (0, "outside")]
)
def test_readyz_schema_outside_envelope(cur: int | None, fragment: str) -> None:
    response = _health_client(FakeDb(), FakeSchema(cur=cur), FakeCollector()).get("/api/v1/readyz")
    assert response.status_code == 503
    schema = response.json()["checks"]["schema_envelope"]
    assert schema["ok"] is False
    assert fragment in schema["detail"]


def test_readyz_without_collector_session() -> None:
    client = _health_client(FakeDb(), FakeSchema(), FakeCollector(collector_connected=False))
    response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["collector"] == {"ok": False, "detail": "no collector session"}


def _state(owner: str | None = None, gen: int | None = None) -> ProjectState:
    return ProjectState(
        project_id="acme",
        version="v3",
        content_hash="sha256:abc",
        base_site_rev=4,
        site_rev=7,
        edit_lock_owner=owner,
        edit_lock_gen=gen,
        applied_at=None,
    )


@dataclass
class FakeStore:
    owner: str | None = None
    gen: int | None = None
    available: bool = True
    calls: list[str] = field(default_factory=list[str])

    def _check(self) -> None:
        if not self.available:
            raise DatabaseUnavailableError("down")

    async def get(self) -> ProjectState:
        self._check()
        return _state(self.owner, self.gen)

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        self._check()
        self.calls.append(f"acquire:{owner}:{generation}")
        if self.owner not in (None, owner):
            raise EditLockConflictError(self.owner)
        self.owner, self.gen = owner, generation
        return 7

    async def release_edit_lock(self, owner: str) -> None:
        self._check()
        self.calls.append(f"release:{owner}")
        if self.owner not in (None, owner):
            raise EditLockConflictError(self.owner)
        self.owner = self.gen = None


def _project_client(store: FakeStore, token: str | None = TOKEN) -> TestClient:
    app = FastAPI()
    app.include_router(build_project_router(store=store, updater_token=token), prefix="/api/v1")
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_get_project() -> None:
    body = _project_client(FakeStore(owner="updater", gen=9)).get("/api/v1/project").json()
    assert body["project_id"] == "acme"
    assert body["site_rev"] == 7
    assert body["edit_locked"] is True
    assert body["edit_lock_gen"] == 9


def test_get_project_db_down_is_503() -> None:
    assert _project_client(FakeStore(available=False)).get("/api/v1/project").status_code == 503


def test_edit_lock_requires_configured_token() -> None:
    client = _project_client(FakeStore(), token=None)
    response = client.post(
        "/api/v1/project/edit-lock", json={"owner": "u", "generation": 1}, headers=AUTH
    )
    assert response.status_code == 503


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_edit_lock_rejects_bad_token(headers: dict[str, str]) -> None:
    store = FakeStore()
    response = _project_client(store).post(
        "/api/v1/project/edit-lock", json={"owner": "u", "generation": 1}, headers=headers
    )
    assert response.status_code == 401
    assert store.calls == []


def test_edit_lock_acquire_conflict_release() -> None:
    store = FakeStore()
    client = _project_client(store)
    acquired = client.post(
        "/api/v1/project/edit-lock", json={"owner": "updater", "generation": 17}, headers=AUTH
    )
    assert acquired.status_code == 200
    assert acquired.json() == {"site_rev": 7}

    other = client.post(
        "/api/v1/project/edit-lock", json={"owner": "x", "generation": 1}, headers=AUTH
    )
    assert other.status_code == 409

    assert client.delete("/api/v1/project/edit-lock?owner=x", headers=AUTH).status_code == 409
    assert client.delete("/api/v1/project/edit-lock?owner=updater", headers=AUTH).status_code == 204
    assert client.delete("/api/v1/project/edit-lock?owner=updater", headers=AUTH).status_code == 204


def test_edit_lock_validates_body() -> None:
    client = _project_client(FakeStore())
    response = client.post(
        "/api/v1/project/edit-lock", json={"owner": "", "generation": -1}, headers=AUTH
    )
    assert response.status_code == 422
