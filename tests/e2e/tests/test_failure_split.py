"""G2 실패 분류(E8, M6-2a 리뷰 r1 H1) — **호출 자체**의 실패(ssh 종료 코드 255·wsl.exe 호출 실패·
스크립트가 알린 실행 환경 탓)는 `infra`, **호출된 제품 스크립트**의 실패(원격 명령의 0 아닌 종료,
WSL 빌드·번들 스크립트의 0 아닌 종료)는 `product`다. 예외 종류가 갈려도 run.py·site.py의 기존
`except` 경로는 이전과 같이 잡는다."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import cast

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
