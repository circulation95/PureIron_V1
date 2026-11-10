"""현장 VM 정의(`SITE_A`·`SITE_B`)와 VM 준비(차분 디스크·seed ISO → 부팅 → cloud-init 대기).

M2d `run.py`는 `SITE_A` 하나를, G2 nightly는 둘 다 쓴다(E2: site-a = BASE, site-b = NEXT). 두 VM은
같은 golden·vSwitch·ssh 키를 쓰고 이름·IP·상태 파일만 다르다. 메모리는
`PUREIRON_E2E_VM_GIB`(기본 3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from harness import buildhost, golden, hyperv, paths
from harness.context import E2EError, InfraError
from harness.site import INCOMING, REMOTE, SITE_SCRIPTS, Site

GOLDEN = paths.E2E / "golden" / "ubuntu-24.04-64g.vhdx"
CLOUD_IMAGE = paths.E2E / "images" / "ubuntu-24.04-server-cloudimg-amd64.img"
SITE_A = Site(
    site_id="site-a",
    vm_name="pureiron-site-a",
    ip="10.77.0.10",
    state_file=paths.E2E / "state.json",
)
SITE_B = Site(
    site_id="site-b",
    vm_name="pureiron-site-b",
    ip="10.77.0.11",
    state_file=paths.E2E / "state-site-b.json",
)
# setup-host.ps1 -Switch 와 같은 이름(기본 pureiron-int).
SWITCH = os.environ.get("PUREIRON_E2E_SWITCH", "pureiron-int")
SSH_WAIT_S = 900
CLOUD_INIT_WAIT_S = 900


def vm_memory_gib(environ: Mapping[str, str]) -> int:
    """현장 VM 메모리(GiB) — `PUREIRON_E2E_VM_GIB`, 기본 3. M6-0 실측: 3 GiB에서 시나리오 16항목
    최저 여유 2.1 GB·OOM 0(`.omc/artifacts/m6/m6-0-report.md`). `hyperv.VmSpec` 기본값은 4
    그대로다. import 시점이 아니라 VM을 만들 때 읽으므로 잘못된 값이 `destroy`·`--help`를 막지
    않는다."""
    raw = environ.get("PUREIRON_E2E_VM_GIB", "3")
    gib = int(raw) if raw.strip().isdecimal() else 0
    if gib < 1:
        raise E2EError(f"PUREIRON_E2E_VM_GIB must be an integer >= 1, got {raw!r}")
    return gib


def site_vm_spec(site: Site, environ: Mapping[str, str]) -> hyperv.VmSpec:
    """현장 VM — 이름·디스크·seed ISO는 `site`, 메모리는 `vm_memory_gib`."""
    return hyperv.VmSpec(
        site.vm_name,
        site.vm_dir / "disk.vhdx",
        site.vm_dir / "seed.iso",
        SWITCH,
        memory_gib=vm_memory_gib(environ),
    )


def check_host() -> None:
    """Hyper-V 권한·vSwitch 점검. 실패는 PC 설정 문제라 `InfraError`다(G2 분류 infra — 제품
    실패로 기록되거나 product 이슈를 열지 않게, M6-2b A3). `run.py`는 이 예외를 잡지 않는다(그대로
    멈춘다)."""
    if not hyperv.available():
        raise InfraError("Hyper-V not available for this user (run tests/e2e/infra/setup-host.ps1)")
    if not hyperv.switch_exists(SWITCH):
        raise InfraError(f"vSwitch {SWITCH} missing (run tests/e2e/infra/setup-host.ps1)")


def boot_vm(site: Site, spec: hyperv.VmSpec, files: dict[str, bytes]) -> float:
    """같은 이름의 VM을 지우고 golden 위 차분 디스크 + seed(`files`)로 새로 띄운다. cloud-init이
    끝나면 VM 쪽 스크립트(`tests/e2e/site/`)를 복사한다. ssh가 붙기까지 걸린 초."""
    hyperv.remove(spec.name, spec.disk)
    golden.write_seed_iso(spec.seed_iso, files)
    hyperv.create_vm(spec, GOLDEN)
    buildhost.reclaim_host_memory()
    hyperv.start(spec.name)
    host = site.host()
    waited = host.wait_ssh(timeout_s=SSH_WAIT_S)
    host.run(
        "cloud-init status --wait >/dev/null; test -f /var/lib/e2e-ready",
        timeout_s=CLOUD_INIT_WAIT_S,
    )
    host.run(f"mkdir -p {REMOTE} {INCOMING}")
    for script in SITE_SCRIPTS:
        host.put(paths.REPO / "tests" / "e2e" / "site" / script, f"{REMOTE}/{script}")
    return waited
