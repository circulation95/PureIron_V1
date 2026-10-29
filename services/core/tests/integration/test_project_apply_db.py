"""`project apply --tar`·`export --with-state`·`restore --from <dir>`(M5-0, plan §3 M5-0-4).

번들과 같은 tar(v1) 적용 → 정의 반영, v2(알람 setpoint 변경·태그 하나 retire) → 반영, 현장 값
변경 뒤 백업 → 실패한 업데이트(v3) → 롤백 경로(restore) → 정의·site_rev·base가 백업 시점
그대로. 같은 hash 재적용은 아무것도 쓰지 않고(현장 드래프트가 남아 있으면 정의 재적용·base
이동 — M5-3 K8), 변조 tar·hash 불일치·적용 도중 실패는 아무것도 바꾸지 않는다.
Updater와 같은 순서로 호스트 CLI를 부른다(상태기계 자체는 updater 단위 테스트).
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.project.export import STATE_FILE, TREE_FILE, tree_tar
from pureiron_project.canonical import dump
from pureiron_project.project import load_project

from .conftest import (
    JsonObject,
    SiteDb,
    Stack,
    above_alarm,
    core_cli,
    holding_tag,
    modbus_device,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.fresh_db]

type Conn = asyncpg.Connection[asyncpg.Record]
type Env = dict[str, str]

EXIT_FAILED = 4


@pytest.fixture
async def conn(site_db: SiteDb) -> AsyncGenerator[Conn]:
    connection = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        yield connection
    finally:
        await connection.close()


@dataclass(frozen=True, slots=True)
class Bundle:
    tar: Path
    content_hash: str
    version: str

    @property
    def data(self) -> bytes:
        return self.tar.read_bytes()


def _bundle(tmp: Path, version: str, *, setpoint: float, tags: int, extra: bool = False) -> Bundle:
    """번들 빌더와 같은 project.tar(바이트 동일성은 deploy/tests/test_build_bundle.py)."""
    devices = {"plc-1": modbus_device("plc-1", "10.0.0.5")}
    tag_lists = {"plc-1": [holding_tag(f"plc-1/r{i:03d}", "plc-1", i) for i in range(tags)]}
    alarms: list[JsonObject] = [
        above_alarm("a-high", "plc-1/r000", setpoint, priority="high"),
        {"id": "a-low", "tag": "plc-1/r001", "type": "below", "setpoint": 5},
    ]
    if extra:  # 실패할 업데이트: 장비 추가 + 알람 하나 제거
        devices["plc-2"] = modbus_device("plc-2", "10.0.0.6")
        tag_lists["plc-2"] = [holding_tag("plc-2/r000", "plc-2", 0)]
        alarms = alarms[:1]
    root = write_project(tmp / version, devices, tag_lists, alarms=alarms)
    project = load_project(root)
    assert project.ok, project.problems
    tar = tmp / f"{version}.tar"
    tar.write_bytes(tree_tar({f.relpath: dump(f.data).encode() for f in project.files}))
    return Bundle(tar, project.content_hash(), version)


def _apply(
    env: Env, bundle: Bundle, *, tar: Path | None = None, content_hash: str | None = None
) -> tuple[int, str]:
    result = core_cli(
        env,
        *("project", "apply", "--tar", str(tar or bundle.tar), "--project-id", "it"),
        *("--version", bundle.version, "--content-hash", content_hash or bundle.content_hash),
    )
    return result.returncode, result.stdout + result.stderr


def _apply_ok(env: Env, bundle: Bundle) -> JsonObject:
    code, output = _apply(env, bundle)
    assert code == 0, output
    parsed: JsonObject = json.loads(output)
    return parsed


@dataclass(frozen=True, slots=True)
class View:
    """정의(활성 여부·정의 JSON)와 project_state 전부(updated_at 제외)."""

    devices: dict[str, tuple[bool, str]]
    tags: dict[str, tuple[bool, str]]
    alarms: dict[str, tuple[bool, str]]
    state: dict[str, object]

    def active_tags(self) -> set[str]:
        return {path for path, (active, _) in self.tags.items() if active}

    def alarm(self, alarm_id: str) -> dict[str, object]:
        active, definition = self.alarms[alarm_id]
        assert active
        parsed: dict[str, object] = json.loads(definition)
        return parsed


async def _rows(conn: Conn, sql: str) -> dict[str, tuple[bool, str]]:
    return {str(r[0]): (bool(r[1]), str(r[2])) for r in await conn.fetch(sql)}


async def _view(conn: Conn) -> View:
    state = await conn.fetchrow(
        "SELECT project_id, version, content_hash, base_site_rev, site_rev, edit_lock_owner,"
        " edit_lock_gen, applied_at, definitions_rev, historian FROM project_state WHERE id"
    )
    assert state is not None
    return View(
        devices=await _rows(conn, "SELECT id, retired_at IS NULL, definition FROM devices"),
        tags=await _rows(conn, "SELECT path, retired_at IS NULL, definition FROM tags"),
        alarms=await _rows(conn, "SELECT alarm_id, retired_at IS NULL, definition FROM alarms"),
        state=dict(state.items()),
    )


async def _files(conn: Conn) -> dict[str, bytes]:
    rows = await conn.fetch("SELECT path, content FROM project_files")
    return {str(r["path"]): bytes(r["content"]) for r in rows}


def _export(env: Env, out: Path) -> tuple[JsonObject, bytes]:
    result = core_cli(env, "project", "export", "--with-state", "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    state: JsonObject = json.loads((out / STATE_FILE).read_text(encoding="utf-8"))
    return state, (out / TREE_FILE).read_bytes()


def _member(data: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        handle = tar.extractfile(name)
        assert handle is not None
        return handle.read().decode()


def _tar_files(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    return {name: _member(data, name).encode() for name in names}


async def test_bundle_apply_update_then_rollback_restores_backup(
    conn: Conn, stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    env = stack.cli_env(fresh_database)
    v1 = _bundle(tmp_path, "v1", setpoint=80, tags=3)
    v2 = _bundle(tmp_path, "v2", setpoint=90, tags=2)
    v3 = _bundle(tmp_path, "v3", setpoint=70, tags=2, extra=True)

    # v1: 정의·파일·메타가 한 번에 들어간다.
    applied = _apply_ok(env, v1)
    assert applied["changed"] is True
    first = await _view(conn)
    assert first.active_tags() == {"plc-1/r000", "plc-1/r001", "plc-1/r002"}
    assert first.alarm("a-high")["setpoint"] == 80
    assert (first.state["version"], first.state["content_hash"]) == ("v1", v1.content_hash)
    assert (first.state["site_rev"], first.state["base_site_rev"]) == (0, 0)
    assert await _files(conn) == _tar_files(v1.data)

    # 같은 hash 재적용 = no-op(applied_at·definitions_rev 포함 그대로).
    again = _apply_ok(env, v1)
    assert (again["changed"], again["definitions"]) == (False, None)
    assert await _view(conn) == first

    # v2: 알람 setpoint 변경 + 태그 r002 retire.
    _apply_ok(env, v2)
    second = await _view(conn)
    assert second.alarm("a-high")["setpoint"] == 90
    assert second.active_tags() == {"plc-1/r000", "plc-1/r001"}
    assert second.tags["plc-1/r002"][0] is False
    assert second.state["definitions_rev"] == int(str(first.state["definitions_rev"])) + 1

    # 현장 운영 값 변경(M5-2 PATCH 흉내): site_rev·base를 서로 다른 값으로, 알람 deadband 변경.
    await conn.execute("UPDATE project_state SET site_rev = 7, base_site_rev = 5 WHERE id")
    await conn.execute(
        "UPDATE alarms SET definition = jsonb_set(definition, '{deadband}', '2.5')"
        " WHERE alarm_id = 'a-high'"
    )
    before = await _view(conn)

    # backed_up: 현재 유효 트리 = 저장된 v2 파일 + 바뀐 운영 값.
    backup_state, backup_tar = _export(env, tmp_path / "backup")
    assert (backup_state["site_rev"], backup_state["base_site_rev"]) == (7, 5)
    assert "deadband: 2.5" in _member(backup_tar, "alarms/alarms.yaml")
    assert {n: b for n, b in _tar_files(backup_tar).items() if n != "alarms/alarms.yaml"} == {
        n: b for n, b in _tar_files(v2.data).items() if n != "alarms/alarms.yaml"
    }

    # 실패할 업데이트 v3가 switching에서 적용됐다(정의·base가 바뀐다).
    _apply_ok(env, v3)
    failed = await _view(conn)
    assert failed.devices["plc-2"][0] is True
    assert failed.alarms["a-low"][0] is False
    assert failed.state["base_site_rev"] == 7

    # rolling_back: 백업 트리 + state 복원 → 백업 시점 그대로(정의 rev만 단조 증가).
    restored = core_cli(env, "project", "restore", "--from", str(tmp_path / "backup"))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    after = await _view(conn)
    assert after.devices["plc-2"][0] is False
    assert {k: v for k, v in after.devices.items() if k != "plc-2"} == before.devices
    assert {k: v for k, v in after.tags.items() if k != "plc-2/r000"} == before.tags
    assert after.alarms == before.alarms
    rev = after.state.pop("definitions_rev")
    assert isinstance(rev, int)
    assert rev > int(str(before.state.pop("definitions_rev")))
    assert after.state == before.state
    assert await _files(conn) == _tar_files(backup_tar)

    # 다시 실행해도 같다(recover 재실행), export → restore → export 바이트 동일.
    rerun = core_cli(env, "project", "restore", "--from", str(tmp_path / "backup"))
    assert rerun.returncode == 0, rerun.stderr
    assert (await _view(conn)).state["definitions_rev"] == rev
    state_again, tar_again = _export(env, tmp_path / "backup-again")
    assert (state_again, tar_again) == (backup_state, backup_tar)


async def test_same_tree_with_site_drafts_reapplies_and_moves_base(
    conn: Conn, stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    """M5-3 K8(M5-2 리뷰 LOW-4): 같은 트리 + 남은 드래프트 → 정의 재적용 + base = site_rev.
    그 뒤 재실행(Updater recover)은 rev가 같아 no-op이다."""
    env = stack.cli_env(fresh_database)
    v1 = _bundle(tmp_path, "v1", setpoint=80, tags=3)
    _apply_ok(env, v1)
    # 현장 PATCH 흉내: 알람 setpoint 변경 + site_rev 3 앞섬(번들은 게이트를 통과했다 = 흡수 완료).
    await conn.execute(
        "UPDATE alarms SET definition = jsonb_set(definition, '{setpoint}', '123')"
        " WHERE alarm_id = 'a-high'"
    )
    await conn.execute("UPDATE project_state SET site_rev = site_rev + 3 WHERE id")
    patched = await _view(conn)
    assert patched.alarm("a-high")["setpoint"] == 123

    reapplied = _apply_ok(env, v1)
    assert reapplied["changed"] is True
    assert reapplied["files_changed"] is False
    after = await _view(conn)
    assert after.alarm("a-high")["setpoint"] == 80
    assert after.state["base_site_rev"] == after.state["site_rev"] == patched.state["site_rev"]
    assert await _files(conn) == _tar_files(v1.data)

    rerun = _apply_ok(env, v1)
    assert (rerun["changed"], rerun["definitions"]) == (False, None)
    assert await _view(conn) == after


async def test_bad_tar_hash_or_mid_apply_failure_changes_nothing(
    conn: Conn, stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    env = stack.cli_env(fresh_database)
    v1 = _bundle(tmp_path, "v1", setpoint=80, tags=3)
    v2 = _bundle(tmp_path, "v2", setpoint=90, tags=2)
    _apply_ok(env, v1)
    baseline = (await _view(conn), await _files(conn))

    tampered = tmp_path / "tampered.tar"
    data = v2.data
    assert data.count(b"setpoint: 90") == 1
    tampered.write_bytes(data.replace(b"setpoint: 90", b"setpoint: 99"))  # 같은 길이, 유효한 tar
    unsafe = tmp_path / "unsafe.tar"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("../escape.yaml")
        tar.addfile(info, io.BytesIO(b""))
    unsafe.write_bytes(buffer.getvalue())
    cases = {
        "hash mismatch": _apply(env, v2, content_hash=v1.content_hash),
        "tampered tar": _apply(env, v2, tar=tampered),
        "unsafe tar": _apply(env, v2, tar=unsafe),
    }
    for name, (code, output) in cases.items():
        assert code == EXIT_FAILED, (name, output)
        assert (await _view(conn), await _files(conn)) == baseline, name
    assert "content hash mismatch" in cases["tampered tar"][1]

    # 적용 도중 실패(정의 반영 뒤 project_files 기록에서) → 트랜잭션 전체가 되돌아간다.
    await conn.execute(
        "CREATE FUNCTION it_fail() RETURNS trigger LANGUAGE plpgsql AS"
        " $$BEGIN RAISE EXCEPTION 'injected project_files failure'; END$$"
    )
    await conn.execute(
        "CREATE TRIGGER it_fail BEFORE INSERT ON project_files"
        " FOR EACH ROW EXECUTE FUNCTION it_fail()"
    )
    code, output = _apply(env, v2)
    assert code == EXIT_FAILED, output
    assert "injected project_files failure" in output
    assert (await _view(conn), await _files(conn)) == baseline
    await conn.execute("DROP TRIGGER it_fail ON project_files")
    applied = _apply_ok(env, v2)
    assert applied["changed"] is True
