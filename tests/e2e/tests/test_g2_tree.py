"""G2-1 재료(E3·E6) — 100태그 트리는 스키마를 통과하고 제품 fixture를 건드리지 않으며, 판정은 태그
하나라도 GOOD이 아니면·장비가 끊기면 실패하고, 시뮬레이터·viewer 조작은 비밀값을 출력하지 않는다."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import site_api
from harness import judges, site, trees
from harness.vm import Vm

from pureiron_project.project import load_project
from pureiron_project.schema import Kind

SIM_PATHS = frozenset(f"sim/r{i:03d}" for i in range(100))


def test_g2_tree_adds_100_simulator_tags_and_passes_the_schema(tmp_path: Path) -> None:
    before = load_project(trees.FIXTURE).content_hash()
    tree = trees.g2_tree(tmp_path / "tree")
    project = load_project(tree)
    assert project.ok, project.problems
    tags = [
        cast(dict[str, Any], t)
        for f in project.files
        if f.kind is Kind.TAG_LIST
        for t in cast(list[object], f.data)
    ]
    sim = [t for t in tags if str(t["path"]).startswith("sim/")]
    assert len(sim) == 100
    assert {t["path"] for t in sim} == SIM_PATHS == trees.g2_tag_paths()
    assert sorted(t["modbus"]["address"] for t in sim) == list(range(100))
    kinds = [t["modbus"]["register_type"] for t in sim]
    assert (kinds.count("input"), kinds.count("holding")) == (50, 50)
    assert {t["device"] for t in sim} == {"plc-1"}
    assert trees.field_endpoints(tree) == (("10.0.0.5", 502),)
    # 제품 fixture는 그대로다
    assert load_project(trees.FIXTURE).content_hash() == before


def tags_body(bad: str | None = None, missing: str | None = None) -> dict[str, object]:
    rows = [
        {"path": p, "quality": "BAD" if p == bad else "GOOD", "value": 1000.0}
        for p in sorted(SIM_PATHS)
        if p != missing
    ]
    return {"tags": [*rows, {"path": "line1/temp", "quality": "BAD", "value": None}]}


def test_all_simulator_tags_good_passes() -> None:
    assert all(judges.judge_tags_good(tags_body(), SIM_PATHS).values())
    assert judges.tags_not_good(tags_body(), SIM_PATHS) == []


def test_one_bad_tag_out_of_100_fails() -> None:
    """변이 (i): 100태그 중 1개 BAD여도 통과하면 안 된다."""
    checks = judges.judge_tags_good(tags_body(bad="sim/r042"), SIM_PATHS)
    assert checks == {"tags_present": True, "tags_good": False}
    assert judges.tags_not_good(tags_body(bad="sim/r042"), SIM_PATHS) == ["sim/r042"]


def test_missing_tag_or_empty_expectation_fails() -> None:
    assert not any(judges.judge_tags_good(tags_body(missing="sim/r099"), SIM_PATHS).values())
    assert not any(judges.judge_tags_good(tags_body(), frozenset()).values())
    assert not any(judges.judge_tags_good({"detail": "not authenticated"}, SIM_PATHS).values())


def test_good_without_value_fails() -> None:
    body = tags_body()
    rows = cast(list[dict[str, object]], body["tags"])
    rows[0]["value"] = None
    assert not judges.judge_tags_good(body, SIM_PATHS)["tags_good"]


def device(state: str = "CONNECTED", connected: bool | None = True, silent: bool = False) -> object:
    return {
        "devices": [
            {"device_id": "plc-1", "state": state, "connected": connected, "silent": silent}
        ]
    }


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        (device(), True),
        (device(state="DISCONNECTED"), False),
        (device(connected=None), False),
        (device(silent=True), False),
        ({"devices": []}, False),
    ],
)
def test_device_health_must_be_connected(body: object, ok: bool) -> None:
    assert judges.judge_device_connected(body, "plc-1") == {"device_connected": ok}


def test_live_needs_both_api_replies() -> None:
    out: dict[str, object] = {"tags": [200, tags_body()], "devices": [200, device()]}
    assert all(judges.judge_live(out, SIM_PATHS, "plc-1").values())
    out["devices"] = [401, {"detail": "not authenticated"}]
    checks = judges.judge_live(out, SIM_PATHS, "plc-1")
    assert (checks["api_ok"], checks["device_connected"]) == (False, False)


def snap(images: dict[str, str], driver: str = "overlay2", ready: int = 200) -> dict[str, object]:
    return {
        "readyz": {"status": ready, "body": {"status": "ready"}},
        "containers": {
            svc: {"image_id": digest, "running": True} for svc, digest in images.items()
        },
        "docker": {"driver": driver, "driver_status": [["Backing Filesystem", "extfs"]]},
    }


def test_installed_compares_the_expected_release_digests() -> None:
    """변이 (v)의 판정 쪽: 실행 이미지가 기대 release(site-b = NEXT)와 다르면 실패."""
    nxt = {"core": "sha256:n1", "collector": "sha256:n2", "web": "sha256:n3", "site-db": "sha256:d"}
    base = {**nxt, "core": "sha256:b1", "collector": "sha256:b2", "web": "sha256:b3"}
    assert all(judges.judge_installed(snap(nxt), nxt).values())
    assert judges.judge_installed(snap(nxt), base)["images_match"] is False
    assert judges.judge_installed(snap(nxt, driver="vfs"), nxt)["overlay2"] is False


class Recorder:
    """`site_api.call` 대신 요청을 기록하고 정해 둔 응답을 낸다."""

    def __init__(self, login_status: int = 200) -> None:
        self.login_status = login_status
        self.calls: list[tuple[str, str, object, str | None]] = []

    def __call__(
        self, method: str, path: str, body: object | None = None, cookie: str | None = None
    ) -> tuple[int, Any, list[str]]:
        self.calls.append((method, path, body, cookie))
        if path == "/api/v1/auth/login":
            return (
                self.login_status,
                {"user": {"username": "e2e-viewer"}},
                ["pureiron_session=tok123; HttpOnly; SameSite=Strict"],
            )
        if path == "/api/v1/tags":
            return 200, tags_body(), []
        if path == "/api/v1/devices":
            return 200, device(), []
        return 204, None, []


def live_args(tmp_path: Path) -> argparse.Namespace:
    password = tmp_path / "pw"
    password.write_text("s3cret-password\n", encoding="utf-8")
    return argparse.Namespace(username="e2e-viewer", password_file=str(password))


def test_site_api_live_reads_tags_and_devices_with_the_session(tmp_path: Path) -> None:
    fake = Recorder()
    out = site_api.live(live_args(tmp_path), fake)
    assert [(m, p) for m, p, _, _ in fake.calls] == [
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/tags"),
        ("GET", "/api/v1/devices"),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert {c[3] for c in fake.calls[1:]} == {"tok123"}
    # VM에서는 JSON 한 줄로 받는다(튜플 → 리스트)
    text = json.dumps(out)
    assert all(judges.judge_live(json.loads(text), SIM_PATHS, "plc-1").values())
    assert "s3cret-password" not in text
    assert "tok123" not in text


def test_site_api_live_stops_after_a_failed_login(tmp_path: Path) -> None:
    fake = Recorder(login_status=401)
    out = site_api.live(live_args(tmp_path), fake)
    assert [p for _, p, _, _ in fake.calls] == ["/api/v1/auth/login"]
    assert "tags" not in out


class FakeVm:
    def __init__(self) -> None:
        self.puts: list[str] = []
        self.commands: list[str] = []

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        del local, timeout_s
        self.puts.append(remote)

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout_s, check, input
        self.commands.append(command)
        return subprocess.CompletedProcess[str](command, 0, "", "")


def test_simulator_runs_outside_compose_on_the_field_port(tmp_path: Path) -> None:
    host = FakeVm()
    site.start_simulator(cast(Vm, host), tmp_path / "simulator.tar", "pureiron/simulator:nX-src")
    assert host.puts == [site.SIM_REMOTE]
    (command,) = host.commands
    for part in (
        "docker load -q -i",
        "--network host",
        "--restart unless-stopped",
        "--user 0:0",
        "-e SIM_PORT=502",
        "pureiron/simulator:nX-src",
    ):
        assert part in command
    assert "compose" not in command


def test_wait_live_returns_once_everything_is_good() -> None:
    replies: list[dict[str, object]] = [
        {},
        {"tags": [200, tags_body(bad="sim/r001")]},
        {"ok": True},
    ]
    clock = iter([0.0, 0.0, 10.0, 20.0, 30.0, 30.0])

    def judge(out: dict[str, object]) -> dict[str, bool]:
        return {"ok": out.get("ok") is True}

    reply, checks, seconds = site.wait_live(
        lambda: replies.pop(0), judge, 300, clock=lambda: next(clock), sleep=lambda _: None
    )
    assert (reply, checks, seconds) == ({"ok": True}, {"ok": True}, 30.0)


def test_wait_live_gives_up_with_the_last_verdict() -> None:
    clock = iter([0.0, 0.0, 200.0, 301.0, 301.0])

    def judge(out: dict[str, object]) -> dict[str, bool]:
        return {"ok": bool(out)}

    _, checks, seconds = site.wait_live(
        dict, judge, 300, clock=lambda: next(clock), sleep=lambda _: None
    )
    assert (checks, seconds) == ({"ok": False}, 301.0)
