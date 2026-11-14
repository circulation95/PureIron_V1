"""G2-5 설치 전 변조 탐침(M6-3b J1, 계획 H5·K6) — site-b 부팅 직후, 실제 설치 전에 설치 번들
사본으로 runbook을 따라가 본다.

설치 번들(약 0.7 GB)은 실제 설치용으로 VM에 한 번 올린 것(`site.install_runbook`의 업로드)을 쓰고,
탐침마다 전용 디렉터리(`PROBE_DIR`)에 VM 안에서 복사한 사본의 멤버 하나만 바꾼다(`site/tamper.py`) —
번들을 호스트에서 여러 번 올리지 않는다. 탐침 둘:

- `tampered_bootstrap`: 사본의 `bootstrap/bootstrap-verify.sh`만 바꾼다 → runbook 2단계(메타데이터만
  추출 → sha256 대조)에서 멈추고 스크립트를 실행하지 않는다.
- `tampered_deb`: 사본의 `debs/*.deb` 하나만 바꾼다 → 정품 스크립트(sha256 = runbook)가 payload
  대조에서 실패한다.

둘 다 끝에 docker가 설치되지 않았는지 보고 탐침 디렉터리를 지운다(실패해도 지운다). 판정은
`judges.judge_script_tamper`·`judge_deb_tamper`(시험 `test_g2_rejections.py`)가 한다. 탐침의 예외는
그 탐침의 결과로만 남고(`run_probes`) 실제 설치를 막지 않는다.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness import judges, paths
from harness.context import TAIL_CHARS
from harness.site import REMOTE, RUNBOOK_META, as_root

PREINSTALL_SITE = "site-b"
"""탐침하는 현장(계획 H5 — site-a에는 하지 않는다)."""
PROBE_DIR = "/root/e2e-preinstall"
TAMPER_REMOTE = f"{REMOTE}/tamper.py"
SCRIPT_PROBE = "tampered_bootstrap"
DEB_PROBE = "tampered_deb"
PROBES: dict[str, str] = {
    SCRIPT_PROBE: judges.BOOTSTRAP_MEMBER,
    DEB_PROBE: "debs/*.deb",
}
"""탐침 → 바꿀 멤버(glob). 이 순서로 돈다."""
COPY_TIMEOUT_S = 600
"""설치 번들 사본(VM 디스크 안 복사 + 멤버 위치 찾기)."""
BOOTSTRAP_TIMEOUT_S = 900
"""`bootstrap-verify.sh`(사본 전체 추출·sha256) — `site.install_runbook`과 같다."""
DOCKER_PROBE = (
    "if command -v docker >/dev/null 2>&1 || "
    "dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q 'ok installed'; "
    "then echo present; else echo absent; fi"
)


class Shell(Protocol):
    """탐침이 쓰는 원격 셸 — `vm.Vm`이 만족한다(단위 시험은 가짜)."""

    def run(
        self, command: str, *, check: bool = True, timeout_s: float = 600
    ) -> subprocess.CompletedProcess[str]: ...

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None: ...


@dataclass(frozen=True)
class Runbook:
    """실제 설치와 같은 runbook 값."""

    install_remote: str
    """VM에 올린 설치 번들(실제 설치가 쓰는 원본 — 탐침은 사본만 바꾼다)."""
    root_fpr: str
    bootstrap_sha: str


def probe(host: Shell, book: Runbook, member: str) -> dict[str, object]:
    """사본 하나를 만들어 `member`(glob)만 바꾸고 runbook을 따라간다: 메타데이터만 추출 → sha256이
    runbook 값과 같을 때만 `bootstrap-verify.sh` 실행 → docker 설치 여부 → 탐침 디렉터리 삭제(예외가
    나도 지운다). 무엇을 했는지 돌려준다(판정은 `judges`)."""
    copy = f"{PROBE_DIR}/bundle.pib"
    found: dict[str, object] = {"member_glob": member}
    started = time.monotonic()
    try:
        out = host.run(
            as_root(
                f"rm -rf {PROBE_DIR} && install -d -m 700 {PROBE_DIR} {PROBE_DIR}/meta && "
                f"cp {book.install_remote} {copy} && "
                f"python3 {TAMPER_REMOTE} {copy} {shlex.quote(member)}"
            ),
            timeout_s=COPY_TIMEOUT_S,
        ).stdout
        found["tamper"] = json.loads(out.strip().splitlines()[-1])
        sha = host.run(
            as_root(
                f"cd {PROBE_DIR}/meta && tar --force-local --no-same-owner --no-same-permissions "
                f"-xf {copy} {RUNBOOK_META} && sha256sum bootstrap/bootstrap-verify.sh"
            ),
            timeout_s=COPY_TIMEOUT_S,
        ).stdout
        found["sha256"] = sha.split()[0] if sha.split() else ""
        if found["sha256"] == book.bootstrap_sha:  # runbook: 같아야 실행한다
            done = host.run(
                as_root(
                    f"cd {PROBE_DIR}/meta && bash bootstrap/bootstrap-verify.sh "
                    f"--root-fpr {book.root_fpr} --bundle {copy} --dest {PROBE_DIR}/dest"
                ),
                check=False,
                timeout_s=BOOTSTRAP_TIMEOUT_S,
            )
            found["bootstrap"] = {"rc": done.returncode, "stderr": done.stderr[-TAIL_CHARS:]}
        found["docker"] = host.run(DOCKER_PROBE).stdout.strip()
    finally:
        gone = host.run(
            as_root(f"rm -rf {PROBE_DIR}; test ! -e {PROBE_DIR} && echo removed"), check=False
        )
        found["removed"] = gone.stdout.strip() == "removed"
        found["seconds"] = round(time.monotonic() - started, 1)
    return found


Probe = Callable[[Shell, Runbook, str], dict[str, object]]
Found = tuple[dict[str, dict[str, object]], dict[str, BaseException]]
"""(탐침 → 결과, 탐침 → 예외)."""
Probes = Callable[[Shell, Runbook], Found]


def run_probes(host: Shell, book: Runbook, *, one: Probe = probe) -> Found:
    """탐침 둘을 차례로 돌린다(탐침 도구를 VM에 먼저 올린다). (탐침 → 결과, 탐침 → 예외) — 한
    탐침의 예외(`Exception`)는 그 탐침에만 남기고 다음 탐침·실제 설치로 넘어간다(K6)."""
    found: dict[str, dict[str, object]] = {}
    errors: dict[str, BaseException] = {}
    try:
        host.put(paths.REPO / "tests" / "e2e" / "site" / "tamper.py", TAMPER_REMOTE)
    except Exception as exc:  # 도구를 못 올리면 두 탐침 모두 그 예외로 남는다
        return {}, dict.fromkeys(PROBES, exc)
    for name, member in PROBES.items():
        try:
            found[name] = one(host, book, member)
        except Exception as exc:  # 탐침 실패는 그 시험의 결과로만 — 실제 설치는 계속한다
            errors[name] = exc
    return found, errors


def judge(name: str, found: dict[str, object], runbook_sha: str) -> dict[str, bool]:
    """탐침 `name`의 판정."""
    if name == SCRIPT_PROBE:
        return judges.judge_script_tamper(found, runbook_sha)
    return judges.judge_deb_tamper(found, runbook_sha)
