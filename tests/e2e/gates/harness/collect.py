"""nightly `collect` 단계(M6-2) — VM마다 `updater.json`·메트릭·`journalctl -u pureiron-updater`·
compose 로그·시뮬레이터 로그를 `<결과>/<site>/`로 모은다. 원격 명령 하나가 실패해도(ssh·시간 초과)
결과에 남기고 다음으로 간다.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

from harness.context import write_json
from harness.site import Site
from harness.vm import VmError

COLLECT_TIMEOUT_S = 60
"""collect의 원격 명령 하나 — 현장 2개, 명령 5개, 60초씩이면 최대 10분."""
REMOTE_LOGS = {
    "updater.json": "sudo cat /var/lib/pureiron/state/updater.json",
    "updater.prom": "sudo cat /var/lib/pureiron/state/metrics/updater.prom",
    "journal-pureiron-updater.txt": "sudo journalctl -u pureiron-updater --no-pager -o short-iso",
    "compose-logs.txt": (
        "for c in $(sudo docker ps -aq --filter label=com.docker.compose.project=pureiron); do "
        "echo \"== $(sudo docker inspect -f '{{.Name}}' $c)\"; "
        "sudo docker logs --timestamps --tail 3000 $c 2>&1; done"
    ),
    "simulator-logs.txt": "sudo docker logs --timestamps --tail 300 e2e-simulator 2>&1",
}


def collect_site(one: Site, dest: Path) -> dict[str, object]:
    """원격 명령마다 출력 파일 하나. 실패(ssh·시간 초과)는 결과에 남기고 다음으로 간다."""
    dest.mkdir(parents=True, exist_ok=True)
    host = one.host()
    got: dict[str, object] = {}
    for name, command in REMOTE_LOGS.items():
        try:
            done = host.run(command, check=False, timeout_s=COLLECT_TIMEOUT_S)
        except (VmError, subprocess.TimeoutExpired) as exc:
            got[name] = f"error: {exc}"
            continue
        (dest / name).write_text(done.stdout, encoding="utf-8")
        got[name] = done.returncode
    return got


def step_collect(out: Path, sites: Iterable[Site]) -> dict[str, object]:
    """현장마다 `collect_site` → `<out>/<site>/`, 요약은 `<out>/collect.json`."""
    collected: dict[str, object] = {
        one.site_id: collect_site(one, out / one.site_id) for one in sites
    }
    write_json(out / "collect.json", collected)
    return collected
