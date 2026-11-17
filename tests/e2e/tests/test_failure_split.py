"""G2 실패 분류(E8, M6-2a 리뷰 r1 H1) — **호출 자체**의 실패(ssh 종료 코드 255·wsl.exe 호출 실패·
스크립트가 알린 실행 환경 탓)는 `infra`, **호출된 제품 스크립트**의 실패(원격 명령의 0 아닌 종료,
WSL 빌드·번들 스크립트의 0 아닌 종료)는 `product`다. 예외 종류가 갈려도 run.py·site.py의 기존
`except` 경로는 이전과 같이 잡는다."""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast

import pytest
import run
from harness import buildhost, bundles, failures, golden, site, vm
from harness.context import Context, InfraError
from harness.release_pair import DEFAULT, nightly
from harness.vm import Vm

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
STARTED = golden.WSL_STARTED


def done(rc: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str]([], rc, stdout, stderr)


# ---------------------------------------------------------------- ssh(Vm.run)


@pytest.mark.parametrize(
    ("command", "rc", "stderr"),
    [
        (
            "sudo bash /root/pureiron-install/install/install.sh --bundle x.pib "
            "--offline-time-confirmed",
            1,
            "install.sh: docker image load failed",
        ),
        ("sudo pureiron-site apply /home/pureiron/incoming/g1.pib", 3, "E_SIG: bad signature"),
    ],
)
def test_remote_command_failure_is_product(command: str, rc: int, stderr: str) -> None:
    """HEAD의 설치 경로(install.sh·pureiron-site)가 0 아닌 코드로 끝나면 제품 결함이다 — infra로
    가면 1회 재시도 뒤 "error"로만 남고 product 이슈가 생기지 않는다(리뷰 H1)."""
    failure = vm.remote_failure(command, done(rc, stderr))
    assert isinstance(failure, vm.RemoteCommandError)
    assert failure.returncode == rc
    assert stderr in str(failure)
    assert failures.classify(failure) == "product"


def test_ssh_exit_255_is_infra() -> None:
    failure = vm.remote_failure(
        "true", done(255, "ssh: connect to host 10.77.0.11 port 22: Connection timed out")
    )
    assert isinstance(failure, vm.VmError)
    assert failures.classify(failure) == "infra"


def test_remote_success_is_not_a_failure() -> None:
    assert vm.remote_failure("true", done(0)) is None


# ---------------------------------------------------------------- WSL(golden.wsl)


@pytest.mark.parametrize(
    ("rc", "stderr"),
    [
        # wsl.exe가 bash를 띄우지 못함(표지 없음) — 예: 0x8007274c
        (1, "Error code: Wsl/Service/CreateInstance/CreateVm/HCS/0x8007274c"),
        (4294967295, "The Windows Subsystem for Linux instance has terminated."),
        # UTF-16으로 나온 wsl.exe 메시지(NUL이 섞인다)
        (1, "\x00".join("Error code: Wsl/Service/0x8007274c")),
        # 스크립트는 시작했지만 wsl.exe가 자기 오류를 냄
        (1, f"{STARTED}\nError code: Wsl/Service/0x8007274c"),
        # 스크립트가 실행 환경 탓을 알림(bundles.INFRA_EXIT)
        (golden.WSL_INFRA_EXIT, f"{STARTED}\ndocker is not responding after the HEAD build failed"),
        # R2-L1: 스크립트 도중 WSL VM이 죽음 — bash는 0~255만 내므로 그 밖의 코드는 wsl.exe 것이다
        (4294967295, f"{STARTED}\nbuilding core...\n"),
        (256, f"{STARTED}\n"),
        (-1, f"{STARTED}\n"),
        # 표지 뒤 wsl.exe의 "instance has terminated" 문구(rc는 bash 범위)
        (1, f"{STARTED}\nThe Windows Subsystem for Linux instance has terminated."),
    ],
)
def test_wsl_call_failure_is_infra(rc: int, stderr: str) -> None:
    failure = golden.wsl_failure(rc, stderr)
    assert isinstance(failure, golden.WslError)
    assert failures.classify(failure) == "infra"


@pytest.mark.parametrize(
    ("rc", "stderr"),
    [
        (
            1,
            f"{STARTED}\nTraceback (most recent call last):\n"
            '  File "deploy/bundle/build_install_bundle.py", line 9\nValueError: bad manifest',
        ),
        (2, f"{STARTED}\nbuild_bundle.py: error: stage_images digest mismatch"),
    ],
)
def test_wsl_script_failure_is_product(rc: int, stderr: str) -> None:
    """WSL에서 HEAD 빌드·번들 스크립트(`bundles.py install`·`site`, `nightly-release`의 빌드 뒤
    단계)가 0 아닌 코드로 끝나면 제품 결함이다."""
    failure = golden.wsl_failure(rc, stderr)
    assert isinstance(failure, golden.WslScriptError)
    assert not isinstance(failure, golden.WslError)
    assert failure.returncode == rc
    assert STARTED not in str(failure)
    assert stderr.splitlines()[-1] in str(failure)
    assert failures.classify(failure) == "product"


def test_wsl_success_is_not_a_failure() -> None:
    assert golden.wsl_failure(0, f"{STARTED}\n") is None


def test_wsl_runs_the_started_marker_before_the_script() -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(cmd)
        return done(1, f"{STARTED}\nTraceback: boom")

    with pytest.raises(golden.WslScriptError, match="boom"):
        golden.wsl("set -euo pipefail; false", runner=runner)
    (cmd,) = calls
    assert cmd[-1] == f"echo {STARTED} >&2; set -euo pipefail; false"


def test_infra_exit_code_is_the_same_on_both_sides(capsys: pytest.CaptureFixture[str]) -> None:
    assert bundles.INFRA_EXIT == golden.WSL_INFRA_EXIT
    with pytest.raises(SystemExit) as raised:
        raise bundles.infra_exit("BASE r5: record or image store missing")
    assert raised.value.code == golden.WSL_INFRA_EXIT
    assert "image store missing" in capsys.readouterr().err


def test_head_copy_failure_is_infra_but_build_steps_are_product(tmp_path: Path) -> None:
    """HEAD git 사본(clone·checkout) 실패는 실행 환경(`WSL_INFRA_EXIT`)이고, 그 뒤
    `nightly-release`가 0 아닌 코드로 끝나면(빌드가 성공한 뒤의 단계 포함) 제품 결함이다."""
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, 5)
    scripts: list[str] = []

    def failing(script: str, *, timeout_s: float) -> str:
        del timeout_s
        scripts.append(script)
        raise golden.WslScriptError("wsl script failed (1): Traceback read_lock", 1)

    with pytest.raises(golden.WslScriptError) as raised:
        buildhost.nightly_release(
            tmp_path, pair, SHA, run_wsl=failing, make_bundle=lambda p: p.touch()
        )
    assert failures.classify(raised.value) == "product"
    (script,) = scripts
    copy, build = script.split(f"|| exit {golden.WSL_INFRA_EXIT}; ")
    assert "git clone" in copy
    assert f"checkout -q {SHA}" in copy
    assert build.startswith(buildhost.bundles_py("nightly-release", buildhost.NIGHTLY_WORK))


def test_repo_sync_failure_is_infra() -> None:
    def failing(script: str, *, timeout_s: float) -> str:
        del script, timeout_s
        raise golden.WslScriptError(
            "wsl script failed (23): rsync: connection unexpectedly closed", 23
        )

    with pytest.raises(InfraError, match="rsync"):
        buildhost.sync_repo(run_wsl=failing)


def test_nightly_uses_its_own_wsl_work_directory() -> None:
    """리뷰 L6: nightly가 `run.py`의 WSL 기록(`work/releases`의 FAULT `r5f.json`)을 다시 맞추지
    않는다."""
    assert buildhost.NIGHTLY_WORK != buildhost.WORK
    assert buildhost.bundles_py("site").endswith(f"--work {buildhost.WORK}")
    assert buildhost.bundles_py("site", buildhost.NIGHTLY_WORK).endswith(
        f"--work {buildhost.NIGHTLY_WORK}"
    )


# ---------------------------------------------------------------- 기존 except 경로


FAILURE_KINDS = [
    vm.RemoteCommandError("ssh `sudo pureiron-site apply x` exited 3: E_SIG", 3),
    vm.VmError("ssh `true` exited 255: connection reset"),
    golden.WslScriptError("wsl script failed (1): Traceback", 1),
    golden.WslError("wsl failed (1): 0x8007274c"),
]


def tmp_ctx(tmp_path: Path) -> Context:
    site_a = dataclasses.replace(run.SITE_A, state_file=tmp_path / "state.json")
    return Context(run_id="t", artifacts=str(tmp_path), site=site_a, pair=DEFAULT)


@pytest.mark.parametrize("exc", FAILURE_KINDS)
def test_run_py_m5_guard_still_catches_every_failure_kind(tmp_path: Path, exc: Exception) -> None:
    ctx = tmp_ctx(tmp_path)

    def broken(c: Context, gens: dict[str, int]) -> None:
        del c, gens
        raise exc

    run.m5_guarded(ctx, {}, broken)
    by_name = {r["name"]: r for r in ctx.results}
    assert all(by_name[name]["ok"] is False for name in run.M5_ITEMS)


@pytest.mark.parametrize("exc", FAILURE_KINDS)
def test_run_py_online_guard_still_catches_every_failure_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """`online_guarded`의 본문과 finally의 `central down`(`except (RuntimeError, …)`) 모두."""
    ctx = tmp_ctx(tmp_path)

    def broken_online(c: Context) -> None:
        del c
        raise exc

    def broken_central(command: str, *args: str) -> dict[str, object]:
        del command, args
        raise exc

    monkeypatch.setattr(run, "step_online", broken_online)
    monkeypatch.setattr(buildhost, "central", broken_central)
    run.online_guarded(ctx, keep_central=False)
    by_name = {r["name"]: r for r in ctx.results}
    assert all(by_name[name]["ok"] is False for name in run.ONLINE_ITEMS)


def test_remote_waits_treat_both_ssh_failure_kinds_as_an_empty_reply() -> None:
    """`wait_ready`·`converge`·`wait_live`·`site_patch`가 잡는 `REMOTE_ERRORS`에 둘 다 있다."""
    assert set(vm.REMOTE_ERRORS) == {vm.VmError, vm.RemoteCommandError}
    assert all(issubclass(e, RuntimeError) for e in (*vm.REMOTE_ERRORS, golden.WslScriptError))
    replies: list[object] = [vm.RemoteCommandError("probe exited 1", 1), {"ok": True}]

    def read() -> dict[str, object]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return cast(dict[str, object], reply)

    clock = iter([0.0, 0.0, 10.0, 10.0])
    reply, checks, _ = site.wait_live(
        read,
        lambda out: {"ok": out.get("ok") is True},
        300,
        clock=lambda: next(clock),
        sleep=lambda _: None,
    )
    assert (reply, checks) == ({"ok": True}, {"ok": True})


class PatchVm:
    """`site_patch`: 계정 생성은 성공, 스크립트 복사 성공, web PATCH 스크립트가 0 아닌 종료."""

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout_s, check, input
        return subprocess.CompletedProcess[str](command, 0, '{"ok": true}', "")

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        del local, remote, timeout_s

    def json(self, command: str, *, timeout_s: float = 120) -> dict[str, object]:
        del timeout_s
        raise vm.RemoteCommandError(f"ssh `{command[:20]}` exited 1: 409 conflict", 1)


def test_site_patch_still_records_a_failed_remote_patch() -> None:
    out, admin = site.site_patch(cast(Vm, PatchVm()))
    assert admin["rc"] == 0
    assert "409 conflict" in str(out["error"])


# ---------------------------------------------------------------- 리뷰 r2(M1·L2)

READY: dict[str, object] = {
    "readyz": {"status": 200},
    "updater": {"attempt": None, "last_attempted_gen": 2, "last_result": "succeeded"},
}
PROBE_FAILURES: list[Exception] = [
    vm.RemoteCommandError("ssh `sudo python3 probe.py` exited 1: core restarting", 1),
    vm.VmError("ssh `sudo python3 probe.py` exited 255: Connection reset"),
    subprocess.TimeoutExpired("ssh", 30),
]


class FlakyProbe:
    """처음 몇 번은 정해 둔 예외, 그다음은 준비된 snap(재시작·부팅 중 probe가 잠깐 실패)."""

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def __call__(self, host: Vm) -> dict[str, object]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return READY


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("failure", PROBE_FAILURES, ids=["remote", "ssh255", "timeout"])
def test_wait_ready_retries_a_failing_probe(failure: Exception) -> None:
    """R2-M1: 원격 명령 실패(`RemoteCommandError`, H1 뒤 product 쪽 예외)도 부팅 중의 일시 실패로
    치고 다시 묻는다 — 되돌려지면 run.py 설치·복원이 probe 한 번 실패로 무너진다(리뷰 변이 B)."""
    probe = FlakyProbe([failure, failure])
    ticks = Ticks()
    snap = site.wait_ready(
        Vm("10.77.0.11", Path("e2e_key")), 300, read=probe, clock=ticks.clock, sleep=ticks.sleep
    )
    assert snap == READY
    assert probe.calls == 3


@pytest.mark.parametrize("failure", PROBE_FAILURES, ids=["remote", "ssh255", "timeout"])
def test_converge_retries_a_failing_probe(failure: Exception) -> None:
    """R2-M1: apply 뒤 수렴 대기도 같다(리뷰 변이 A — run.py의 모든 시나리오가 쓴다)."""
    probe = FlakyProbe([failure])
    ticks = Ticks()
    snap, waited = site.converge(
        Vm("10.77.0.11", Path("e2e_key")), 2, 600, read=probe, clock=ticks.clock, sleep=ticks.sleep
    )
    assert snap == READY
    assert (probe.calls, waited) == (2, 5.0)


def test_wait_ready_gives_up_at_the_timeout() -> None:
    ticks = Ticks()
    snap = site.wait_ready(
        Vm("10.77.0.11", Path("e2e_key")),
        12,
        read=FlakyProbe(PROBE_FAILURES * 5),
        clock=ticks.clock,
        sleep=ticks.sleep,
    )
    assert snap == {}


def no_call(src: Path, name: str) -> subprocess.CompletedProcess[str]:
    pytest.fail(f"build must not run: {src} {name}")


def no_docker() -> bool:
    pytest.fail("docker is only checked after a failed build")


def release(
    tmp_path: Path,
    *,
    head: str = SHA,
    build_head: Callable[[Path, str], subprocess.CompletedProcess[str]] = no_call,
    docker_ok: Callable[[], bool] = no_docker,
    store: Path | None = None,
    records: Path | None = None,
) -> dict[str, object]:
    return bundles.nightly_release(
        tmp_path / "work-nightly",
        tmp_path / "n1a2b3c4-src",
        sha=SHA,
        name="n1a2b3c4",
        version="2026.11.1+n1a2b3c4",
        base="r5",
        sim_out=tmp_path / "sim.tar",
        git_head=lambda src: head,
        build_head=build_head,
        docker_ok=docker_ok,
        store_root=lambda: store or tmp_path / "no-store",
        records=records or tmp_path / "no-records",
    )


def test_nightly_release_copy_at_the_wrong_commit_is_infra_exit(tmp_path: Path) -> None:
    """R2-L2: `nightly-release`의 실행 환경 탓 3길이 `INFRA_EXIT`(75)로 끝난다 — exit 1이면 Windows
    쪽이 product로 읽고 재시도하지 않는다(리뷰 변이 E). ① 사본이 다른 커밋."""
    with pytest.raises(SystemExit) as raised:
        release(tmp_path, head="f" * 40)
    assert raised.value.code == bundles.INFRA_EXIT


def test_nightly_release_build_failure_is_infra_only_when_docker_is_down(tmp_path: Path) -> None:
    """② 빌드 실패 뒤 Docker 무응답은 infra(75), Docker가 살아 있으면 제품 실패(`ok: false`)."""

    def failed_build(src: Path, name: str) -> subprocess.CompletedProcess[str]:
        return done(2, "build_images.sh: core failed to compile")

    with pytest.raises(SystemExit) as raised:
        release(tmp_path, build_head=failed_build, docker_ok=lambda: False)
    assert raised.value.code == bundles.INFRA_EXIT
    result = release(tmp_path, build_head=failed_build, docker_ok=lambda: True)
    assert (result["ok"], result["stage"], result["rc"]) == (False, "build", 2)


@pytest.mark.parametrize("missing", ["record", "store"])
def test_nightly_release_without_the_base_record_or_store_is_infra_exit(
    tmp_path: Path, missing: str
) -> None:
    """③ BASE 기록이나 실제 image store가 없으면 infra(75)."""
    records, store = tmp_path / "releases", tmp_path / "store"
    records.mkdir()
    store.mkdir()
    if missing != "record":
        (records / "r5.json").write_text("{}", encoding="utf-8")
    if missing != "store":
        (store / "r5").mkdir()
    with pytest.raises(SystemExit) as raised:
        release(tmp_path, build_head=lambda src, name: done(0), store=store, records=records)
    assert raised.value.code == bundles.INFRA_EXIT


def timed_out(*args: object, **kwargs: object) -> NoReturn:
    raise subprocess.TimeoutExpired("docker save", 600)


def release_after_build(
    tmp_path: Path,
    *,
    docker_ok: Callable[[], bool],
    git_head: Callable[[Path], str] = lambda src: SHA,
    build_head: Callable[[Path, str], subprocess.CompletedProcess[str]] = lambda src, name: done(0),
) -> dict[str, object]:
    """BASE 기록·store가 있는 상태에서 빌드 뒤 단계(`finish`)가 시간 초과로 멈추는
    nightly-release."""
    records, store = tmp_path / "releases", tmp_path / "store"
    (store / "r5").mkdir(parents=True)
    records.mkdir()
    (records / "r5.json").write_text('{"name": "r5", "images": {}}', encoding="utf-8")
    return bundles.nightly_release(
        tmp_path / "work-nightly",
        tmp_path / "n1a2b3c4-src",
        sha=SHA,
        name="n1a2b3c4",
        version="2026.11.1+n1a2b3c4",
        base="r5",
        sim_out=tmp_path / "sim.tar",
        git_head=git_head,
        build_head=build_head,
        docker_ok=docker_ok,
        store_root=lambda: store,
        records=records,
        link=lambda link, target: None,
        finish=timed_out,
    )


def test_head_build_timeout_is_product_when_docker_answers(tmp_path: Path) -> None:
    """N3: 빌드가 시간 제한을 넘겼는데 Docker가 답하면 HEAD가 빌드에서 멈춘 것(제품, ok false)."""

    def hung(src: Path, name: str) -> NoReturn:
        raise subprocess.TimeoutExpired("build_images.sh", bundles.HEAD_BUILD_TIMEOUT_S)

    result = release(tmp_path, build_head=hung, docker_ok=lambda: True)
    assert (result["ok"], result["stage"], result["rc"]) == (False, "build", "timeout")
    with pytest.raises(SystemExit) as raised:
        release(tmp_path, build_head=hung, docker_ok=lambda: False)
    assert raised.value.code == bundles.INFRA_EXIT


def test_a_git_timeout_in_the_copy_is_infra(tmp_path: Path) -> None:
    def slow_git(src: Path) -> NoReturn:
        raise subprocess.TimeoutExpired("git", bundles.GIT_TIMEOUT_S)

    with pytest.raises(SystemExit) as raised:
        release_after_build(tmp_path, docker_ok=no_docker, git_head=slow_git)
    assert raised.value.code == bundles.INFRA_EXIT


@pytest.mark.parametrize(("answers", "infra"), [(True, False), (False, True)])
def test_a_timeout_after_the_build_follows_the_docker_rule(
    tmp_path: Path, answers: bool, infra: bool
) -> None:
    """빌드 뒤 파생·저장의 시간 초과: Docker가 답하면 제품 스크립트 실패(exit 1), 아니면 인프라."""
    with pytest.raises(SystemExit) as raised:
        release_after_build(tmp_path, docker_ok=lambda: answers)
    assert (raised.value.code == bundles.INFRA_EXIT) is infra
    assert raised.value.code != 0


def test_build_time_limits_fit_inside_the_wsl_call() -> None:
    """단계 한도는 멈춘 한 단계를 잡는 것이다 — 빌드 한도가 Windows 쪽 WSL 호출 한도보다 짧아야
    빌드 멈춤이 제품(ok false)으로 분류된다(같거나 길면 늘 WSL 시간 초과 = 인프라)."""
    assert bundles.HEAD_BUILD_TIMEOUT_S < buildhost.NIGHTLY_TIMEOUT_S
    assert (
        bundles.DOCKER_BUILD_TIMEOUT_S
        < bundles.DOCKER_SAVE_TIMEOUT_S
        < bundles.HEAD_BUILD_TIMEOUT_S
    )


# ---------------------------------------------------------------- 설치 번들 빌드의 apt·DNS(4b B0)

APT_DNS = (
    "W: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/noble/InRelease  "
    "Temporary failure resolving 'archive.ubuntu.com'\n"
    "E: Unable to locate package python3.12-venv\n"
    "subprocess.CalledProcessError: Command '('bash', 'deploy/build/build_updater.sh', "
    "'/w/updater')' returned non-zero exit status 100."
)


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        (APT_DNS, failures.INFRA),
        ("curl: (6) Could not resolve host: download.docker.com\nexit status 100", failures.INFRA),
        ("socket.gaierror: [Errno -3] Temporary failure in name resolution", failures.INFRA),
        # 문구 없는 apt 실패(패키지 이름이 틀림 등)는 그대로 제품
        (
            "E: Unable to locate package python3.12-venv\nreturned non-zero exit status 100.",
            failures.PRODUCT,
        ),
    ],
)
def test_install_bundle_dns_failures_are_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, kind: str
) -> None:
    """WSL·Docker Desktop 재시작 직후 이름 풀이가 흔들리면 `apt-get update`는 경고만 내고 다음
    `apt-get install`이 exit 100으로 실패한다 — 문구로 infra를 가른다."""

    def broken_wsl(script: str, *, timeout_s: float = 0) -> str:
        del script, timeout_s
        raise golden.WslScriptError(f"wsl script failed (1): {stderr}", 1)

    monkeypatch.setattr(buildhost, "wsl", broken_wsl)
    with pytest.raises((InfraError, golden.WslScriptError)) as caught:
        buildhost.install_bundle(tmp_path, "2026.11.1")
    assert failures.classify(caught.value) == kind
