"""M4 마감 온라인 단계(★D5) 판정·중앙 헬퍼.

판정 fixture는 실측값이다(`fixtures/m4c-fleet-rows.json`): 워커가 WSL에서 `central.py up`으로 중앙
c0를 띄우고, VM Updater와 같은 형식의 서명 heartbeat·보고를 보내며 단계마다 `central.py fleet`
응답을 그대로 남겼다(control-web을 멈춘 뒤 36 s의 stale은 `--direct`, control-api 컨테이너 안에서
조회). 각 판정은 자기 단계의 행에서 통과하고 다른 단계의 행을 넣으면 실패해야 한다.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

import central
import pytest
import run
import vm
from release_pair import BASE, RELEASES

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MEASURED = cast(
    dict[str, dict[str, object]],
    json.loads((FIXTURES / "m4c-fleet-rows.json").read_text(encoding="utf-8")),
)
CA_PEM = (FIXTURES / "m4c-central-root.crt").read_text(encoding="ascii")
CA_SHA256 = (
    "9C:6A:74:B1:53:92:D9:4E:E4:13:B7:D8:09:B6:C7:8C:"
    "27:5B:03:D3:82:90:49:A2:97:5A:28:A1:AD:28:B6:20"
)
"""같은 실측의 `openssl x509 -noout -fingerprint -sha256` 값."""


def row(stage: str) -> dict[str, object]:
    found = run.fleet_row(MEASURED[stage])
    assert found, stage
    return found


REGISTERED, PUBLISHED, ONLINE_G1 = row("registered"), row("published"), row("online_g1")
REPORTED, STOPPED_EARLY = row("reported_g2"), row("stopped_direct_early")
STALE, BACK = row("stale_direct"), row("back_online")
PUT = MEASURED["put"]
ALL_ROWS = {
    "registered": REGISTERED,
    "published": PUBLISHED,
    "online_g1": ONLINE_G1,
    "reported_g2": REPORTED,
    "stopped_direct_early": STOPPED_EARLY,
    "stale_direct": STALE,
    "back_online": BACK,
}


def test_measured_rows_are_the_pair_base_and_online_gen() -> None:
    assert RELEASES[BASE] == "2026.11.0"
    assert run.ONLINE_GEN == 2
    assert run.sub(PUBLISHED, "desired").get("product_version") == RELEASES[BASE]
    assert MEASURED["stale_direct"]["via"] == "control-api"
    assert MEASURED["online_g1"]["via"] == "caddy"


def test_fleet_row_needs_exactly_one_site_row() -> None:
    assert run.fleet_row(MEASURED["online_g1"])["connection"] == "online"
    assert run.fleet_row({"status": 404, "body": {"detail": "unknown site"}}) == {}
    body = cast(list[object], MEASURED["online_g1"]["body"])
    assert run.fleet_row({"status": 200, "body": body * 2}) == {}
    other = [{**cast(dict[str, object], body[0]), "site": "site-b"}]
    assert run.fleet_row({"status": 200, "body": other}) == {}


# ---------------------------------------------------------------- ① HTTPS heartbeat


def poll(r: dict[str, object], **over: object) -> dict[str, bool]:
    kwargs: dict[str, object] = {"ca_sha256": CA_SHA256, "vm_ca_sha256": CA_SHA256}
    kwargs |= {"site_id": run.SITE, **over}
    return run.judge_online_poll(
        r,
        ca_sha256=str(kwargs["ca_sha256"]),
        vm_ca_sha256=str(kwargs["vm_ca_sha256"]),
        site_id=kwargs["site_id"],
    )


def test_online_poll_passes_on_first_heartbeat_row() -> None:
    assert all(poll(ONLINE_G1).values())


@pytest.mark.parametrize(
    ("stage", "failing"),
    [
        ("registered", {"connection_online", "heartbeat_seq", "current_g1"}),
        ("published", {"connection_online", "heartbeat_seq", "current_g1"}),
        ("reported_g2", {"current_g1"}),  # g2를 이미 적용한 뒤의 행은 c0(g1) 상태가 아니다
        ("stale_direct", {"connection_online", "current_g1"}),
    ],
)
def test_online_poll_fails_on_other_stages(stage: str, failing: set[str]) -> None:
    checks = poll(ALL_ROWS[stage])
    assert {k for k, ok in checks.items() if not ok} == failing


def test_online_poll_needs_matching_ca_and_registration() -> None:
    assert not poll(ONLINE_G1, vm_ca_sha256="AA:" + CA_SHA256[3:])["ca_fingerprint"]
    assert not poll(ONLINE_G1, ca_sha256="", vm_ca_sha256="")["ca_fingerprint"]
    assert not poll(ONLINE_G1, site_id=None)["registered"]


REPORT_CLEARED: dict[str, object] = {
    **ONLINE_G1,
    "last_heartbeat_seq": 1,
    "last_report_seq": 2,
    "current": {
        "gen": 1,
        "product": None,
        "project_version": None,
        "reported_at": "2026-11-08T21:03:07Z",
        "source": "online",
    },
}
"""VM run 20261108T205941Z의 ① 행: 폐쇄망 g1의 outbox 성공 보고(seq 2)가 첫 heartbeat(seq 1) 다음에
들어와 current를 덮었다 — 중앙에 g1 게시 번들이 없어 버전이 null(control-api `reports.py`)."""
HEARTBEAT_REFILLED: dict[str, object] = {
    **REPORT_CLEARED,
    "last_heartbeat_seq": 3,
    "current": {**run.sub(ONLINE_G1, "current"), "reported_at": "2026-11-08T21:03:17Z"},
}
"""이어서 다음 heartbeat(seq 3, poll 10 s)가 manifest의 product·project_version으로 다시 채운 행."""


def test_online_poll_fails_while_g1_report_cleared_the_versions() -> None:
    checks = poll(REPORT_CLEARED)
    assert {k for k, ok in checks.items() if not ok} == {"current_g1"}
    assert all(poll(HEARTBEAT_REFILLED).values())


def test_online_poll_waits_past_the_report_cleared_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """① 대기는 `connection == online`이 아니라 판정의 행 항목 전체로 끝난다 — 실측 행(online이지만
    current 버전 null)에서 멈추지 않고 heartbeat가 채운 두 번째 행에서 끝나 통과로 기록한다
    (변이: 대기 조건을 connection만으로 되돌리면 첫 행에서 끝나 실패)."""
    replies = [REPORT_CLEARED, HEARTBEAT_REFILLED]
    asked: list[tuple[str, ...]] = []

    def fake_central(command: str, *args: str) -> dict[str, object]:
        asked.append((command, *args))
        return {"status": 200, "body": [replies[min(len(asked), len(replies)) - 1]]}

    def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(run, "central", fake_central)
    monkeypatch.setattr(run.time, "sleep", no_sleep)
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")  # 실제 state.json 보호
    ctx = run.Context(run_id="t", artifacts=str(tmp_path))
    setup: dict[str, object] = {
        "ca_sha256": CA_SHA256,
        "vm_ca_sha256": CA_SHA256,
        "registered": {"site_id": run.SITE},
    }
    run.online_poll(ctx, setup)
    assert asked == [("fleet", "--site", run.SITE)] * 2
    (result,) = ctx.results
    assert result["name"] == run.ITEM_ONLINE_POLL
    assert result["ok"] is True
    assert result["row"] == HEARTBEAT_REFILLED


def test_online_poll_waits_on_row_checks_only() -> None:
    """CA 지문·등록은 행과 무관해 대기 조건에서 빠진다(어긋나도 시간 초과까지 기다리지 않는다)."""
    unrelated = {"ca_fingerprint", "registered"}
    assert set(run.ONLINE_POLL_ROW_CHECKS) == set(poll(ONLINE_G1)) - unrelated


def test_ca_fingerprint_matches_openssl_and_python() -> None:
    line = f"sha256 Fingerprint={CA_SHA256}\n"
    assert run.ca_fingerprint(line) == CA_SHA256
    assert run.ca_fingerprint(line.lower()) == CA_SHA256
    assert run.ca_fingerprint("") == ""
    # 중앙 쪽 지문(central.py, DER SHA-256)이 openssl과 같은 표기여야 VM 값과 비교된다
    assert central.fingerprint(CA_PEM) == CA_SHA256


# ---------------------------------------------------------------- ② 게시 · ③ 보고


def test_online_published_needs_201_and_desired_gen() -> None:
    assert all(run.judge_online_published(PUT, PUBLISHED).values())
    failing = {k for k, ok in run.judge_online_published(PUT, REGISTERED).items() if not ok}
    assert failing == {"desired_gen", "desired_product"}
    conflict: dict[str, object] = {"status": 409, "body": {"detail": "generation 2 <= published 2"}}
    assert not run.judge_online_published(conflict, PUBLISHED)["put_201"]
    # desired는 g2인데 게시 수위가 다르면(다른 gen이 이미 게시됨) 실패 — 15분 실측 PR이 gen 3
    assert not run.judge_online_published(PUT, {**PUBLISHED, "max_published_gen": 3})["desired_gen"]


def test_online_report_passes_after_report_and_back_online() -> None:
    assert all(run.judge_online_report(REPORTED).values())
    assert all(run.judge_online_report(BACK).values())


@pytest.mark.parametrize(
    ("stage", "failing"),
    [
        ("online_g1", {"current", "last_attempt", "attempt_source"}),
        (
            "published",
            {"current", "current_source", "last_attempt", "attempt_source", "connection_online"},
        ),
        ("stale_direct", {"connection_online"}),
    ],
)
def test_online_report_fails_on_other_stages(stage: str, failing: set[str]) -> None:
    checks = run.judge_online_report(ALL_ROWS[stage])
    assert {k for k, ok in checks.items() if not ok} == failing


def test_online_report_needs_online_source_and_success() -> None:
    attempt = run.sub(REPORTED, "last_attempt")
    offline = {**REPORTED, "last_attempt": {**attempt, "source": "offline"}}
    assert not run.judge_online_report(offline)["attempt_source"]
    failed = {**REPORTED, "last_attempt": {**attempt, "result": "failed"}}
    assert not run.judge_online_report(failed)["last_attempt"]
    current = run.sub(REPORTED, "current")
    imported = {**REPORTED, "current": {**current, "source": "offline"}}
    assert not run.judge_online_report(imported)["current_source"]


# ---------------------------------------------------------------- ④ 중앙 중지


WANT = {"core": "sha256:c", "collector": "sha256:o", "web": "sha256:w", "site-db": "sha256:d"}
DURING: dict[str, object] = {
    "readyz": {"status": 200},
    "containers": {
        svc: {"image_id": digest, "running": True, "started_at": f"2026-11-09T00:00:0{i}Z"}
        for i, (svc, digest) in enumerate(WANT.items())
    },
    "updater": {
        "last_result": "succeeded",
        "alarm": None,
        "applied_gen": 2,
        "last_attempted_gen": 2,
        "attempt": None,
    },
}
STARTED = "2026-11-09T00:00:00Z"


def outage(**over: object) -> dict[str, bool]:
    args: dict[str, object] = {
        "during": DURING,
        "stale": STALE,
        "back": BACK,
        "updater_active": True,
        "restarts": (0, 0),
        "core_started": (STARTED, STARTED),
        **over,
    }
    return run.judge_outage(
        cast(dict[str, object], args["during"]),
        want_digests=WANT,
        stale=cast(dict[str, object], args["stale"]),
        back=cast(dict[str, object], args["back"]),
        updater_active=bool(args["updater_active"]),
        restarts=cast(tuple[int, int], args["restarts"]),
        core_started=cast(tuple[object, object], args["core_started"]),
    )


def test_outage_passes_on_measured_stale_and_back_rows() -> None:
    assert all(outage().values())


def test_outage_stale_needs_the_wait() -> None:
    # 멈춘 직후(1 s)의 행은 아직 online — 3 x poll을 기다려야 stale이다(실측)
    assert STOPPED_EARLY["connection"] == "online"
    assert not outage(stale=STOPPED_EARLY)["stale"]


def test_outage_back_online_needs_a_newer_heartbeat() -> None:
    # 재기동 뒤 행 대신 멈추기 전 행(online이지만 last_seen이 stale과 같다)은 복귀가 아니다
    assert not outage(back=REPORTED)["back_online"]
    assert not outage(back=STALE)["back_online"]
    assert not outage(back={})["back_online"]


@pytest.mark.parametrize(
    ("over", "failing"),
    [
        ({"updater_active": False}, "updater_active"),
        ({"restarts": (0, 1)}, "updater_not_restarted"),
        ({"restarts": (-1, -1)}, "updater_not_restarted"),
        ({"core_started": (STARTED, "2026-11-09T00:01:00Z")}, "core_not_restarted"),
        ({"core_started": (None, None)}, "core_not_restarted"),
    ],
)
def test_outage_site_side_flags(over: dict[str, object], failing: str) -> None:
    checks = outage(**over)
    assert {k for k, ok in checks.items() if not ok} == {failing}


@pytest.mark.parametrize(
    ("patch", "failing"),
    [
        ({"readyz": {"status": 503}}, "site_ready"),
        ({"updater": {**cast(dict[str, object], DURING["updater"]), "alarm": "g2: x"}}, "no_alarm"),
        (
            {"updater": {**cast(dict[str, object], DURING["updater"]), "attempt": {"g": 3}}},
            "no_new_attempt",
        ),
        (
            {"updater": {**cast(dict[str, object], DURING["updater"]), "last_result": "failed"}},
            "no_new_attempt",
        ),
    ],
)
def test_outage_updater_state_flags(patch: dict[str, object], failing: str) -> None:
    checks = outage(during={**DURING, **patch})
    assert not checks[failing]


def test_seen_after_compares_times_not_strings() -> None:
    a: dict[str, object] = {"last_seen": "2026-11-08T15:50:41.866529Z"}
    b: dict[str, object] = {
        "last_seen": "2026-11-08T15:50:42Z"
    }  # 소수점 없는 표기가 문자열로는 앞에 온다
    assert run.seen_after(b, a)
    assert not run.seen_after(a, b)
    assert not run.seen_after({"last_seen": None}, a)
    assert not run.seen_after(b, {"last_seen": "not a time"})


# ---------------------------------------------------------------- central.py(WSL 헬퍼)


def test_central_values_follow_d9() -> None:
    env = central.compose_env()
    assert central.PROJECT == "pureiron-e2e-central"
    assert central.RELEASE == "c0"
    assert (
        env["PUREIRON_CENTRAL_BIND"],
        env["PUREIRON_CENTRAL_HOST"],
        env["PUREIRON_CENTRAL_DEFAULT_SNI"],
    ) == ("10.77.0.1",) * 3
    assert env["PUREIRON_CENTRAL_HTTPS_PORT"] == "28443"
    assert env["PUREIRON_CONTROL_POLL_S"] == str(run.CENTRAL_POLL_S)
    assert central.BASE_URL == run.CENTRAL_URL
    assert env["PUREIRON_CENTRAL_SECRETS_DIR"] == str(central.SECRETS)
    # 비밀 디렉터리는 저장소·artifacts 밖(홈 아래)
    assert not central.SECRETS.is_relative_to(central.ROOT)


def test_central_c0_record_exists_for_the_images() -> None:
    record = json.loads((central.RECORD_DIR / "c0.json").read_text(encoding="utf-8"))
    assert record["kind"] == "central"
    assert set(cast(dict[str, object], record["images"])) == set(central.CENTRAL_SERVICES)


def test_register_keeps_the_site_token_out_of_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "site-token-should-not-be-printed"
    monkeypatch.setattr(central, "SECRETS", tmp_path)

    def fake_cli(*args: str, stdin: bytes | None = None) -> dict[str, object]:
        del args, stdin
        return {"site_id": "site-a", "fingerprint": "f" * 64, "next_seq": 1, "token": secret}

    monkeypatch.setattr(central, "_cli_json", fake_cli)
    enrollment = tmp_path / "enrollment.json"
    enrollment.write_text("{}", encoding="utf-8")
    out = central.register(enrollment)
    assert secret not in json.dumps(out)
    assert out["token_file"] == str(tmp_path / "site-a.token")
    assert (tmp_path / "site-a.token").read_text(encoding="utf-8") == secret + "\n"
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "site-a.token").stat().st_mode) == 0o600


def test_status_lists_token_paths_not_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(central, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(central, "CA_FILE", tmp_path / "central-root.crt")
    (tmp_path / "central-root.crt").write_text(CA_PEM, encoding="ascii")
    info = central.status()
    assert info["ca_sha256"] == CA_SHA256
    files = cast(dict[str, str], info["token_files"])
    assert set(files) == {"admin", "ci", "publish_viewer", "viewer"}
    assert all(Path(p).parent == tmp_path / "secrets" for p in files.values())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 파일 모드")
def test_write_secret_modes(tmp_path: Path) -> None:
    central.write_secret(tmp_path / "a", "x")
    central.write_secret(tmp_path / "b", "y", mode=0o644)
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "b").st_mode) == 0o644


def test_basic_auth_password_goes_through_stdin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """L1: control-web 화면 비밀번호는 `caddy hash-password`의 stdin으로만 — `docker run` 인자
    (호스트 `ps`·컨테이너 프로세스 인자)에 없다. caddy는 끝 줄바꿈이 있어야 읽는다."""
    password = "ui-password-should-not-be-an-argument"
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess[str](argv, 0, "$2a$14$hash\n", "")

    monkeypatch.setattr(central.subprocess, "run", fake_run)
    assert central.basic_auth_line(password) == f"{central.UI_USER} $2a$14$hash"
    argv = cast(list[str], seen["argv"])
    assert not any(password in a for a in argv)
    assert "--plaintext" not in argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert cast(dict[str, object], seen["kw"])["input"] == password + "\n"


class FakeVm:
    """`site_online`이 부르는 Vm 메서드만 — 원격 명령과 stdin을 기록한다."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str | None]] = []

    def put(self, local: Path, remote: str) -> None:
        del local, remote

    def get(self, remote: str, local: Path) -> None:
        del remote
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("{}", encoding="utf-8")

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout_s, check
        self.runs.append((command, input))
        out = ""
        if "openssl x509" in command:
            out = f"sha256 Fingerprint={CA_SHA256}\n"
        elif "grep -E" in command:
            out = f"PUREIRON_CONTROL_API={run.CENTRAL_URL}\nPUREIRON_POLL_INTERVAL_S=10\n"
        return subprocess.CompletedProcess[str](command, 0, out, "")


def test_site_online_passes_the_site_token_by_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L2: 현장 token은 `pureiron-site secret set`의 stdin으로만 간다 — 어느 원격 명령 줄에도,
    결과 기록에도 없다(변이 M26: token을 `secret set` 인자로)."""
    token = "site-token-must-stay-off-the-command-line"
    monkeypatch.setattr(run, "BUNDLES", tmp_path)

    def fake_central(command: str, *args: str) -> dict[str, object]:
        del command, args
        return {"site_id": run.SITE, "next_seq": 1}

    def fake_wsl(script: str, *, timeout_s: float = 3600) -> str:
        del script, timeout_s
        return token + "\n"

    monkeypatch.setattr(run, "central", fake_central)
    monkeypatch.setattr(run, "wsl", fake_wsl)
    ctx = run.Context(run_id="t", artifacts=str(tmp_path))
    host = FakeVm()
    ca = tmp_path / "central-root.crt"
    ca.write_text(CA_PEM, encoding="ascii")
    setup = run.site_online(ctx, cast(run.Vm, host), ca, CA_SHA256)
    assert all(token not in command for command, _ in host.runs)
    fed = [(command, given) for command, given in host.runs if given is not None]
    assert fed == [("sudo pureiron-site secret set control_api_token", token + "\n")]
    assert token not in json.dumps(setup)
    assert setup["vm_ca_sha256"] == CA_SHA256


def test_vm_run_feeds_input_to_ssh_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2: `Vm.run(input=)`은 ssh 표준 입력으로만 넘긴다(명령 줄 아님). input이 없으면 stdin을
    닫는다(콘솔 stdin 공유 금지 — S4 crash). 변이 M24(input 무시)를 잡는다."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        return subprocess.CompletedProcess[str](argv, 0, "", "")

    def fake_tool(name: str) -> str:
        return name

    monkeypatch.setattr(vm, "_tool", fake_tool)
    monkeypatch.setattr(vm.subprocess, "run", fake_run)
    host = vm.Vm("10.77.0.10", Path("id_ed25519"))
    host.run("sudo pureiron-site secret set control_api_token", input="vm-secret-x9\n")
    host.run("true")
    (fed_argv, fed), (_, closed) = calls
    assert fed["input"] == "vm-secret-x9\n"
    assert fed["stdin"] is None
    assert not any("vm-secret-x9" in a for a in fed_argv)
    assert closed["input"] is None
    assert closed["stdin"] is subprocess.DEVNULL


def test_online_guarded_survives_a_hung_central_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`online_guarded` finally의 `central down`이 시간 초과(`TimeoutExpired`)여도 report로
    넘어간다 — 온라인 항목 실패 기록은 그대로 남는다."""
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")  # 실제 state.json 보호
    ctx = run.Context(run_id="t", artifacts=str(tmp_path))

    def broken_online(c: run.Context) -> None:
        del c
        raise run.VmError("ssh exited 255")

    def hung_central(command: str, *args: str) -> dict[str, object]:
        raise subprocess.TimeoutExpired(["central.py", command, *args], 3600)

    monkeypatch.setattr(run, "step_online", broken_online)
    monkeypatch.setattr(run, "central", hung_central)
    run.online_guarded(ctx, keep_central=False)
    by_name = {r["name"]: r for r in ctx.results}
    assert all(by_name[name]["ok"] is False for name in run.ONLINE_ITEMS)
