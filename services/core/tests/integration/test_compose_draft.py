"""compose 현장 드래프트 수직 경로(M5-2 작업 4 + M5-3a 회수): 트리 apply → admin PATCH 3회 →
`GET /project` site_rev +3 → 드래프트 게이트(absorbs 없음 `E_DRAFT`, `absorbs[site]` = site_rev
통과) → 잠금 중 PATCH 423 → Updater `export_draft`(실제 core 컨테이너 `project export --draft`) →
`open_draft` 서명 검증 + 트리에 PATCH 값 → 벤더 `pureiron-project absorb`(repo = 처음 apply한 트리
사본, 키 = Updater `enroll`이 쓴 enrollment 파일) → `gate`(absorbs = site_rev) 통과 → 회수한 트리
apply → `base_site_rev == site_rev`, 정의 = PATCH 값.

compose 하네스는 Updater 서비스·호스트 설치(`/var/lib/pureiron`, `docker compose -p pureiron`)를
쓰지 않는다. 그래서 Updater 쪽은 `pureiron_updater.draft.export_draft`를 테스트 compose 스택에
연결한 `Compose` 어댑터로 부른다 — `ComposeCli.run_core`와 같은 argv 모양(`run --rm --no-deps -T
--volume <host>:<container>`)이다. 게이트는 번들을 만들지 않고 `verify_bundle` ⑥·Updater
`locked`가 쓰는 `check_draft_gate`를 이 스택의 실제 `GET /project` 값으로 부른다(번들 검증 순서는
bundle-tools·updater 단위 테스트). 회수한 트리 apply도 번들 대신 Updater와 같은 `project apply
--tar`(호스트 CLI)로 한다.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.sitesign import open_draft
from pureiron_bundle.verify import DraftState, check_draft_gate
from pureiron_core.project.apply import load_tree, read_tar
from pureiron_core.project.export import STATE_FILE, TREE_FILE, tree_tar
from pureiron_project.canonical import dump
from pureiron_project.cli import main as project_cli
from pureiron_project.project import load_project
from pureiron_updater.draft import export_draft
from pureiron_updater.enroll import enroll
from pureiron_updater.ports import ComposeTarget
from pureiron_updater.settings import Settings
from pureiron_updater.state import StateStore, UpdaterState

from .conftest import (
    CORE_API,
    SIM_ALARMS,
    SIM_TAGS,
    Stack,
    compose,
    core_cli,
    ensure_user,
    holding_tag,
    login_token,
    modbus_device,
    wait_http,
    write_project,
)

pytestmark = pytest.mark.integration

SITE = "site-it"
ADMIN = "it-draft-admin"


def _call(
    method: str, path: str, token: str, body: Mapping[str, object] | None = None
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        f"{CORE_API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _apply_tree(stack: Stack, root: Path, tar: Path, version: str) -> None:
    project = load_project(root)
    assert project.ok, project.problems
    tar.write_bytes(tree_tar({f.relpath: dump(f.data).encode() for f in project.files}))
    applied = core_cli(
        stack.cli_env("pureiron"),
        *("project", "apply", "--tar", str(tar), "--project-id", "it", "--version", version),
        *("--content-hash", project.content_hash()),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr


def _apply_sim_tree(stack: Stack, tmp: Path) -> Path:
    """`apply_sim_project`와 같은 정의를 트리로 apply(project_files가 있어야 드래프트가 있다).
    돌려주는 디렉터리 = 고객 repo 역할(벤더가 회수할 트리)."""
    root = write_project(
        tmp / "sim-tree",
        {"sim": modbus_device("sim", "simulator", 5020)},
        {"sim": [holding_tag(f"sim/r{i:03d}", "sim", i) for i in range(SIM_TAGS)]},
        alarms=SIM_ALARMS,
    )
    _apply_tree(stack, root, tmp / "sim-tree.tar", "draft-it")
    return root


@dataclass(frozen=True, slots=True)
class StackCompose:
    """테스트 스택에 붙인 Updater `Compose`(이 테스트는 `run_core`만 쓴다)."""

    stack: Stack

    def config_images(self, target: ComposeTarget) -> Mapping[str, str]:
        raise AssertionError("not used")

    def up(self, target: ComposeTarget, services: Sequence[str] = ()) -> None:
        raise AssertionError("not used")

    def running_images(self, target: ComposeTarget) -> Mapping[str, str]:
        raise AssertionError("not used")

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
        writable: Mapping[Path, str] | None = None,
    ) -> str:
        volumes: list[str] = []
        for host, container in sorted((mounts or {}).items()):
            volumes += ["--volume", f"{host}:{container}:ro"]
        for host, container in sorted((writable or {}).items()):
            # 비root 테스트는 컨테이너 uid(10001)로 chown 할 수 없어 쓰기 권한을 연다(백업 mount
            # 테스트와 같은 한계 — 실제 소유·0700 경로는 root 서비스, r4 VM 게이트).
            host.chmod(0o777)
            volumes += ["--volume", f"{host}:{container}"]
        result = compose(
            self.stack.compose_env, "run", "--rm", "--no-deps", "-T", *volumes, "core", *args
        )
        return result.stdout

    def dump_db(self, target: ComposeTarget, out: Path) -> None:
        raise AssertionError("not used")

    def down(self, target: ComposeTarget) -> None:
        raise AssertionError("not used")

    def release_lock_db(self, target: ComposeTarget) -> None:
        raise AssertionError("not used")


def _member(data: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        handle = tar.extractfile(name)
        assert handle is not None
        return handle.read().decode()


def _site(tmp: Path) -> tuple[Settings, bytes, Path]:
    """등록된 Updater 상태(g1 적용됨), site 공개키, enrollment 파일(벤더가 설치 때 받는 파일)."""
    root_pub = tmp / "root.pub"
    keys.write_public_key(root_pub, keys.public_key(keys.generate_seed()))
    settings = Settings(
        site_id=SITE,
        root=tmp / "site",
        root_pubkey_file=root_pub,
        core_url=CORE_API,
        env_file=tmp / "updater.env",
    )
    enrollment = enroll(settings, SITE, now=datetime.now(UTC))
    StateStore(settings.state_dir).save(UpdaterState(applied_gen=1))
    return settings, keys.public_key(keys.read_private_key(settings.site_key_file)), enrollment


@pytest.mark.compose
def test_site_patch_gate_signed_draft_and_absorb(
    stack: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wait_http(f"{CORE_API}/readyz", timeout_s=120)
    repo = tmp_path / "repo"
    shutil.copytree(_apply_sim_tree(stack, tmp_path), repo)
    ensure_user(stack, ADMIN, "admin")
    token = login_token(ADMIN)
    status, before = _call("GET", "/project", token)
    assert status == 200
    base = before["site_rev"]
    assert isinstance(base, int)
    assert before["base_site_rev"] == base  # 트리 apply = 기준점

    patches = [
        ("/project/alarms/sim-r000-high", {"setpoint": 1234.5}),
        ("/project/tags/sim/r001", {"eu": "bar"}),
        ("/project/tags/sim/r002", {"record": {"deadband": 2.5}}),
    ]
    for i, (path, body) in enumerate(patches, start=1):
        status, out = _call("PATCH", path, token, body)
        assert status == 200, out
        assert out["site_rev"] == base + i
    status, project = _call("GET", "/project", token)
    assert (project["site_rev"], project["base_site_rev"]) == (base + 3, base)

    draft = DraftState(site_rev=base + 3, base_site_rev=base)
    with pytest.raises(BundleRejectedError) as rejected:
        check_draft_gate(SITE, {}, draft)  # absorbs 없는 번들
    assert rejected.value.code is RejectCode.E_DRAFT
    check_draft_gate(SITE, {SITE: base + 3}, draft)  # 흡수된 드래프트 번들은 통과

    lock = _call(
        "POST", "/project/edit-lock", stack.updater_token, {"owner": "it-draft", "generation": 1}
    )
    assert lock[0] == 200, lock
    try:
        assert _call("PATCH", "/project/alarms/sim-r000-high", token, {"setpoint": 1.0}) == (
            423,
            {"detail": "E_EDIT_LOCKED"},
        )
    finally:
        assert _call("DELETE", "/project/edit-lock?owner=it-draft", stack.updater_token)[0] == 204

    settings, public, enrollment = _site(tmp_path)
    out = tmp_path / "usb" / "draft.tar"
    result = export_draft(settings, StackCompose(stack), out, now=datetime.now(UTC))
    payload, tree = open_draft(out.read_bytes(), public, site_id=SITE)
    assert payload == result.payload
    assert (payload["site_id"], payload["site_rev"], payload["base_site_rev"]) == (
        SITE,
        base + 3,
        base,
    )
    assert load_tree(read_tar(tree)).content_hash == payload["content_hash"]
    assert "setpoint: 1234.5" in _member(tree, "alarms/alarms.yaml")
    tags = _member(tree, "tags/sim.yaml")
    assert "eu: bar" in tags
    assert "deadband: 2.5" in tags

    # M5-3a: 벤더가 repo(처음 apply한 트리)에 회수 → 게이트 → 회수한 트리 apply.
    capsys.readouterr()
    absorb_args = ["absorb", "--draft", str(out), "--site", SITE, "--site-key", str(enrollment)]
    assert project_cli([*absorb_args, str(repo)]) == 0
    report = capsys.readouterr().out.splitlines()
    assert report[-1].startswith(f"Absorbs-Draft: {SITE} rev={base + 3} sha256=")
    assert f"site key: {SITE} {keys.fingerprint(public)}" in report[0]
    assert "alarms/alarms.yaml: alarm sim-r000-high setpoint: 1000 -> 1234.5" in report
    rev_args = ["--site-rev", str(base + 3), "--base-site-rev", str(base)]
    assert project_cli(["gate", str(repo), "--site", SITE, *rev_args]) == 0

    _apply_tree(stack, repo, tmp_path / "absorbed.tar", "absorb-it")
    status, applied = _call("GET", "/project", token)
    assert status == 200
    assert (applied["site_rev"], applied["base_site_rev"]) == (base + 3, base + 3)
    exported = tmp_path / "after-absorb"
    result_export = core_cli(
        stack.cli_env("pureiron"), "project", "export", "--with-state", "--out", str(exported)
    )
    assert result_export.returncode == 0, result_export.stdout + result_export.stderr
    state = json.loads((exported / STATE_FILE).read_text(encoding="utf-8"))
    assert (state["version"], state["site_rev"], state["base_site_rev"]) == (
        "absorb-it",
        base + 3,
        base + 3,
    )
    effective = (exported / TREE_FILE).read_bytes()
    assert load_tree(read_tar(effective)).content_hash == load_project(repo).content_hash()
    assert "setpoint: 1234.5" in _member(effective, "alarms/alarms.yaml")
    tags = _member(effective, "tags/sim.yaml")
    assert "eu: bar" in tags
    assert "deadband: 2.5" in tags
