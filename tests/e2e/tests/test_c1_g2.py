"""c1 쌍(M6-4a A4·★P1) — 중앙 DB 덤프·복원(`central.py dump/restore`, compose는 가짜), fleet 세션
준비(`fleet_session.prepare_c1`: 도구 브랜치 → 중앙 → publish 호스트 → 정리·사슬 → 태그 → 온라인 →
기준선 → c1)와 복원 순서(`restore_c1`), c0 시험만 고르면 중앙을 띄우지 않는 것."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fleet_fakes import FakeGitHub, FakeRemote, repos, site_yaml
from g2_fakes import scenario_session
from harness import baseline, central, fleet, fleet_session, g2, paths
from harness.context import E2EError, InfraError

SAVED: dict[str, dict[str, object]] = {
    "site-a": {"desired.generation": 6, "max_published_gen": 6, "last_heartbeat_seq": 40},
    "site-b": {"desired.generation": 1, "max_published_gen": 1, "last_heartbeat_seq": 41},
}


# ---------------------------------------------------------------- central.py dump/restore


def fleet_reply(view: dict[str, dict[str, object]]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for site, values in view.items():
        row: dict[str, object] = {"site": site, "desired": {}, "current": {}}
        for key, value in values.items():
            part, _, name = key.rpartition(".")
            target = row if not part else row[part]
            assert isinstance(target, dict)
            target[name] = value
        rows.append(row)
    return {"status": 200, "body": rows}


def test_fleet_view_and_mismatch() -> None:
    view = central.fleet_view(fleet_reply(SAVED))
    assert view["site-a"]["max_published_gen"] == 6
    assert central.fleet_mismatch(view, view) == []
    later = {**view, "site-a": {**view["site-a"], "max_published_gen": 7}}
    assert central.fleet_mismatch(view, later) == ["site-a.max_published_gen"]
    assert central.fleet_mismatch(view, {"site-a": view["site-a"]}) == ["site-b"]
    assert central.fleet_mismatch({}, {}) == ["<saved view empty>"]  # 못 읽은 값은 일치가 아니다
    assert central.fleet_view({"status": 401, "body": []}) == {}


class Compose:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, now: dict[str, object]):
        self.calls: list[tuple[str, ...]] = []
        self.now = now
        monkeypatch.setattr(central, "DUMPS", tmp_path / "dumps")
        monkeypatch.setattr(central, "compose_raw", self.raw)
        monkeypatch.setattr(central, "compose", self.text)
        monkeypatch.setattr(central, "fleet_direct", self.fleet)
        monkeypatch.setattr(central.time, "sleep", self.sleep)

    def text(self, *args: str, stdin: bytes | None = None, check: bool = True) -> str:
        return self.raw(*args, stdin=stdin, check=check).decode()

    def fleet(self, site: str | None) -> dict[str, object]:
        return self.now

    def sleep(self, seconds: float) -> None:
        return

    def raw(self, *args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
        self.calls.append(args)
        return b"PGDMP-data" if "pg_dump" in args else b""


def test_dump_writes_a_0600_dump_and_the_fleet_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = Compose(monkeypatch, tmp_path, fleet_reply(SAVED))
    done = central.dump("c1-R-0001")
    data = tmp_path / "dumps" / "c1-R-0001.dump"
    assert data.read_bytes() == b"PGDMP-data"
    if os.name != "nt":
        assert stat.S_IMODE(data.stat().st_mode) == 0o600
        assert stat.S_IMODE(data.parent.stat().st_mode) == 0o700
    assert done["fleet"] == central.fleet_view(fleet_reply(SAVED))
    assert fake.calls == [
        ("exec", "-T", "control-db", "pg_dump", "-U", "pureiron", "-d", "pureiron_control", "-Fc")
    ]


def test_restore_stops_both_restores_starts_the_api_and_checks_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = Compose(monkeypatch, tmp_path, fleet_reply(SAVED))
    central.dump("c1-R-0001")
    fake.calls.clear()
    done = central.restore("c1-R-0001")
    assert fake.calls[0] == ("stop", "control-web", "control-api")
    assert fake.calls[1][:4] == ("exec", "-T", "control-db", "pg_restore")
    assert "--clean" in fake.calls[1]
    assert "--single-transaction" in fake.calls[1]
    assert fake.calls[2] == ("start", "control-api")
    assert ("start", "control-web") not in fake.calls  # VM을 되돌린 뒤 부른 쪽이 켠다
    assert done["fleet_match"] is True


def test_restore_refuses_a_fleet_that_differs_from_the_dump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """변이: 복원 뒤 `/fleet` 일치 확인을 빼면 control-api가 다른 상태로 떠도 c1이라고
    믿는다(K2)."""
    fake = Compose(monkeypatch, tmp_path, fleet_reply(SAVED))
    central.dump("c1-R-0001")
    fake.now = fleet_reply({**SAVED, "site-a": {**SAVED["site-a"], "max_published_gen": 7}})
    with pytest.raises(central.CentralError, match=r"site-a\.max_published_gen"):
        central.restore("c1-R-0001")


def test_restore_needs_a_dump_from_this_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Compose(monkeypatch, tmp_path, fleet_reply(SAVED))
    with pytest.raises(central.CentralError, match="this session"):
        central.restore("c1-other-session")
    with pytest.raises(central.CentralError):
        central.dump("../escape")


# ---------------------------------------------------------------- prepare_c1


def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[g2.Session, FakeGitHub]:
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "bundles")
    s = scenario_session(tmp_path)
    s.records = {s.pair.next: {"name": s.pair.next}, s.pair.fault: {"name": s.pair.fault}}

    def publish(repo: str, sha: str, event: str) -> tuple[str, str]:
        lines: list[str] = []
        for path, data in sorted(gh.files_at(repo, sha).items()):
            gen = baseline.file_gen(data)
            lines.append(f"::notice::{path[6:-5]} gen {gen} 게시(x.pib, 1 bytes)")
        return "success", "\n".join(
            [*lines, "publish: 게시 2, 건너뜀 0, 최신 0, 실패 0, 규칙 위반 0"]
        )

    gh = FakeGitHub(publish=publish)
    gh.commits[s.head_sha] = {"files": {"README.md": b"v1"}, "parents": []}
    gh.refs[(fleet.V1_REPO, "heads/master")] = s.head_sha
    gh.seed(fleet.FLEET_REPO, {"sites/site-a.yaml": site_yaml("site-a", 5)})
    gh.seed(fleet.PROJECT_REPO, {"project.yaml": b"project_id: change-me\n"})
    return s, gh


def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[g2.Session, FakeGitHub, FakeRemote, fleet_session.FleetSession]:
    s, gh = world(tmp_path, monkeypatch)
    fake = FakeRemote(s, chain=[gh.refs[(fleet.FLEET_REPO, "heads/main")]])
    fake.dump_fleet = SAVED
    fs = fleet_session.prepare_c1(s, remote=fake.remote(), repos=repos(gh), results=tmp_path)
    return s, gh, fake, fs


def test_prepare_c1_builds_the_pair_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, gh, fake, fs = prepared(tmp_path, monkeypatch)
    order = [e for e in fake.events if not e.startswith(("mark", "central fleet"))]
    assert order[:6] == [
        "central up",
        "host trust",
        "host tokens",
        "host images",
        "wsl publish-viewer-token",
        "host chain",
    ]
    i = order.index
    assert i("c0 site-a") < i("online site-a") < i("converge site-a g6")
    assert i("online site-b") < i("converge site-b g1")
    assert i("converge site-b g1") < i("central stop-web") < i("central dump")
    assert i("central dump") < i("checkpoint pureiron-site-a c1") < i("central start-web")
    assert i("checkpoint pureiron-site-b c1") < i("central start-web")
    assert "central down" not in fake.events
    assert gh.refs[(fleet.V1_REPO, "heads/e2e/nightly-tool")]  # 도구 브랜치가 먼저
    assert fs.gens == {"site-a": 6, "site-b": 1}
    assert fs.c1_fleet == SAVED
    assert fs.c1_nft == {"site-a": {"ext_out_v4": 11}, "site-b": {"ext_out_v4": 11}}
    assert fs.baseline_sha == gh.refs[(fleet.FLEET_REPO, "heads/main")]
    saved = json.loads((tmp_path / "fleet" / "session.json").read_text("utf-8"))
    assert saved["c1_name"] == fs.c1_name
    assert "SECRET" not in json.dumps(saved)


def test_every_session_makes_its_own_c1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """변이: 재시도 세션이 첫 세션의 c1(덤프 이름·체크포인트)을 쓰면 다른 중앙·다른 VM의 상태로
    시작한다. 세션마다 새 이름으로 덤프하고 체크포인트를 다시 찍는다."""
    _, _, first_fake, first = prepared(tmp_path / "a", monkeypatch)
    _, _, second_fake, second = prepared(tmp_path / "b", monkeypatch)
    assert first.c1_name != second.c1_name
    for fake in (first_fake, second_fake):
        assert "central dump" in fake.events
        assert "checkpoint pureiron-site-a c1" in fake.events


def test_a_failed_prepare_brings_the_central_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, gh = world(tmp_path, monkeypatch)
    fake = FakeRemote(s, chain=[None])  # 사슬 없음 → infra
    with pytest.raises(InfraError, match="no approval chain"):
        fleet_session.prepare_c1(s, remote=fake.remote(), repos=repos(gh), results=tmp_path)
    assert fake.events[-1] == "central down"


def test_a_baseline_apply_failure_is_a_product_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, gh = world(tmp_path, monkeypatch)
    fake = FakeRemote(s, chain=[gh.refs[(fleet.FLEET_REPO, "heads/main")]])
    fake.applied["site-b"] = (0, "failed")
    with pytest.raises(E2EError, match="site-b baseline g1 not applied"):
        fleet_session.prepare_c1(s, remote=fake.remote(), repos=repos(gh), results=tmp_path)


# ---------------------------------------------------------------- restore_c1


def restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sites: tuple[str, ...]
) -> tuple[FakeRemote, fleet_session.FleetSession]:
    s, gh, fake, fs = prepared(tmp_path, monkeypatch)
    fake.chain = [fs.baseline_sha]  # 기준선 publish가 사슬을 merge 커밋으로 남긴다
    fake.events.clear()
    fake.fleet_rows = {"site-a": {"last_heartbeat_seq": 41}, "site-b": {"last_heartbeat_seq": 42}}
    s.sites["site-a"].ctx.nft_last = {"ext_out_v4": 99}
    fleet_session.restore_c1(s, fs, sites, remote=fake.remote(), repos=repos(gh), results=tmp_path)
    assert s.sites["site-a"].ctx.nft_last == ({"ext_out_v4": 11} if sites else {"ext_out_v4": 99})
    return fake, fs


def test_restore_c1_keeps_the_web_down_while_vms_and_central_go_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """사슬 대조 → control-web 멈춤 → VM c1 → 중앙 restore → control-web 시작 → heartbeat. 변이:
    중앙 restore를 빼면 VM은 c1인데 중앙은 앞 시나리오의 desired를 가져 F8이 돌아온다."""
    fake, fs = restored(tmp_path, monkeypatch, ("site-a", "site-b"))
    core = [e for e in fake.events if not e.startswith("central fleet")]
    assert core == [
        "host chain",
        "central stop-web",
        "vm-restore site-a c1",
        "vm-restore site-b c1",
        "central restore",
        "central start-web",
    ]
    assert fs.restores == 1
    assert fs.chain[-1]["action"] == "none"


def test_restore_c1_for_e_approval_moves_only_the_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _ = restored(tmp_path, monkeypatch, ())
    assert not any(e.startswith("vm-restore") for e in fake.events)
    assert "central restore" in fake.events


def test_restore_c1_needs_a_matching_central_and_fresh_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, gh, fake, fs = prepared(tmp_path, monkeypatch)
    fake.chain = [fs.baseline_sha]
    fake.restore_reply = {"fleet_match": False}
    with pytest.raises(InfraError, match="does not match"):
        fleet_session.restore_c1(s, fs, ("site-a",), remote=fake.remote(), repos=repos(gh))
    assert fake.events[-1] == "central start-web"  # 실패해도 입구를 다시 연다
    fake.restore_reply = {"fleet_match": True}
    fake.fleet_rows = {"site-a": {"last_heartbeat_seq": 40}}  # c1 수위 그대로 — 보고가 안 들어옴
    with pytest.raises(InfraError, match="not as expected"):
        fleet_session.restore_c1(s, fs, ("site-a",), remote=fake.remote(), repos=repos(gh))


# ---------------------------------------------------------------- 중앙을 띄울지(order)


def test_a_c0_only_selection_never_prepares_the_central(tmp_path: Path) -> None:
    """변이: c0 시나리오만 고른 재시도(평일 site-b 묶음 등)가 중앙·기준선 PR을 만들면 약 6분과
    fleet-test 쓰기가 낭비된다."""
    s = scenario_session(tmp_path)
    called: list[str] = []

    def prepare(session: g2.Session) -> fleet_session.FleetSession:
        called.append("prepare")
        raise AssertionError("must not prepare")

    c0 = ["test_g2_rollback.py::test_g2_4_rollback", "test_g2_no_external.py::x[site-a]"]
    assert fleet_session.open_fleet(s, c0, prepare=prepare) is None
    assert called == []
    with pytest.raises(AssertionError):
        fleet_session.open_fleet(
            s, [*c0, "test_g2_approval.py::test_e_approval[run_failed]"], prepare=prepare
        )
    assert called == ["prepare"]
