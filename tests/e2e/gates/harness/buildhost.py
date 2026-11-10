"""빌드 호스트(WSL runner) 조작 — 저장소 사본 동기화, 번들 빌드(`bundles.py`),
중앙(`central.py`).

WSL 쪽 스크립트는 저장소 사본(`paths.WSL_HOME`/repo)에서
`python tests/e2e/gates/harness/<이름>.py`로 돈다. 다른 모듈은 이 모듈의 함수를 `buildhost.wsl`처럼
모듈 속성으로 부른다(단위 시험이 바꿔 끼운다).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from harness import bundles, golden, judges, paths
from harness.context import E2EError, InfraError

if TYPE_CHECKING:
    from harness.context import Context
    from harness.release_pair import ReleasePair

BUNDLES_PY = "tests/e2e/gates/harness/bundles.py"
CENTRAL_PY = "tests/e2e/gates/harness/central.py"
ONLINE_TIMEOUT_S = 180
INSTALL_TIMEOUT_S = 3600
NIGHTLY_TIMEOUT_S = 3600
"""HEAD 이미지 빌드(core·collector·web·simulator, BuildKit 캐시 있음) + 라벨 파생 + 저장."""
GIT_TIMEOUT_S = 120
WORK = f"{paths.WSL_HOME}/work"
"""`run.py`의 WSL 번들 작업 디렉터리(`bundles.py --work`)."""
NIGHTLY_WORK = f"{paths.WSL_HOME}/work-nightly"
"""G2 nightly의 WSL 번들 작업 디렉터리 — `run.py`의 `WORK/releases`(FAULT 기록 `r5f.json`)를
nightly가 다시 맞추지 않게 나눈다(M6-2a 리뷰 L6). BASE image store는 둘 다 같은 실제 store를
가리킨다."""
NIGHTLY_SRC = f"{paths.WSL_HOME}/nightly-src"
"""WSL의 HEAD git 사본 — 빌드 스크립트가 커밋을 읽으므로 rsync 사본(.git 없음)이 아니라
clone이다."""
RUNBOOK_FPR = re.compile(r"root 지문[^:]*:\s*([0-9a-f]{64})")
RUNBOOK_BOOTSTRAP = re.compile(r"bootstrap-verify\.sh sha256:\s*([0-9a-f]{64})")


class HeadBuildError(E2EError):
    """HEAD가 빌드되지 않는다 — 제품 결함(`product`, D7)이다."""


def wsl(script: str, *, timeout_s: float = 3600) -> str:
    epoch = subprocess.run(
        ["git", "-C", str(paths.REPO), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    prologue = (
        "set -euo pipefail; export PATH=$HOME/.local/bin:$PATH; "
        f"export SOURCE_DATE_EPOCH={epoch}; cd {paths.WSL_HOME}/repo; "
    )
    return golden.wsl(prologue + script, timeout_s=timeout_s)


def sync_repo(run_wsl: Callable[..., str] = golden.wsl) -> None:
    """Windows 저장소 → WSL 사본(rsync) + `uv sync`. 사본 동기화는 제품 스크립트가 아니라서 스크립트
    실패도 `InfraError`다(같은 lock은 workflow의 `uv sync --locked`가 먼저 검사한다)."""
    try:
        run_wsl(
            f"set -euo pipefail; export PATH=$HOME/.local/bin:$PATH; "
            f"mkdir -p {paths.WSL_HOME}/repo; "
            f"rsync -a --delete --exclude .git --exclude .venv --exclude node_modules "
            f"--exclude __pycache__ --exclude artifacts '{golden.wsl_path(paths.REPO)}/' "
            f"{paths.WSL_HOME}/repo/; "
            f"cd {paths.WSL_HOME}/repo && uv sync --all-packages --locked -q",
            timeout_s=1800,
        )
    except golden.WslScriptError as exc:
        raise InfraError(f"WSL repo sync failed: {exc}") from exc


def reclaim_host_memory() -> None:
    """WSL(번들 빌드·Docker Desktop)의 페이지 캐시를 비워 VM 시작·복원에 쓸 호스트 메모리를
    돌려받는다. WSL VM은 캐시를 Windows에 늦게 돌려줘 4 GiB VM 복원이 0x8007000E로
    실패했다(첫 실측)."""
    subprocess.run(
        [
            "wsl",
            "-d",
            golden.WSL_DISTRO,
            "-u",
            "root",
            "--exec",
            "sh",
            "-c",
            "sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )


def bundles_py(command: str, work: str = WORK) -> str:
    """WSL `bundles.py <command> --work …` 명령 줄(인자는 `command` 뒤에 이어 붙인다)."""
    return f"uv run --no-sync python {BUNDLES_PY} {command} --work {work}"


def install_bundle(out: Path, release: str, *, work: str = WORK) -> tuple[Path, str, str]:
    """설치 번들 실빌드(`bundles.py install`, 이미지는 싣지 않는다 — `release`는 manifest·파일
    이름에만). (번들, runbook root 지문, bootstrap-verify.sh sha256)."""
    wsl(
        f"{bundles_py('install', work)} --out '{golden.wsl_path(out)}' --release {release}",
        timeout_s=INSTALL_TIMEOUT_S,
    )
    pib = out / f"pureiron-install-{release}.pib"
    runbook = pib.with_name(pib.stem + ".runbook.txt").read_text(encoding="utf-8")
    fpr, boot = RUNBOOK_FPR.search(runbook), RUNBOOK_BOOTSTRAP.search(runbook)
    if fpr is None or boot is None:
        raise E2EError(f"runbook values not found in {pib.name}.runbook.txt")
    return pib, fpr[1], boot[1]


def git_bundle(path: Path) -> None:
    """저장소 HEAD를 git bundle 파일로(Orca worktree처럼 WSL git이 gitdir을 못 읽어도 clone할 수
    있게)."""
    try:
        subprocess.run(
            ["git", "-C", str(paths.REPO), "bundle", "create", str(path), "HEAD"],
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise InfraError(f"git bundle failed: {exc.stderr!r}") from exc


def nightly_release(
    out: Path,
    pair: ReleasePair,
    head_sha: str,
    *,
    run_wsl: Callable[..., str] | None = None,
    make_bundle: Callable[[Path], None] = git_bundle,
    work: str = NIGHTLY_WORK,
) -> dict[str, object]:
    """HEAD를 WSL에서 빌드해 NEXT(`pair.next`) 기록을 만든다(`bundles.py nightly-release`).
    시뮬레이터 tar는 `out/simulator.tar`. NEXT 기록(JSON)을 돌려준다. 빌드 실패는
    `HeadBuildError`(제품), 빌드가 성공한 뒤의 스크립트 실패는 `golden.WslScriptError`(제품),
    WSL 호출 실패·git 사본 실패(`WSL_INFRA_EXIT`)는 `golden.WslError`(인프라)다."""
    out.mkdir(parents=True, exist_ok=True)
    head = out / "head.bundle"
    make_bundle(head)
    script = (
        f"{{ rm -rf {NIGHTLY_SRC} && git clone -q '{golden.wsl_path(head)}' {NIGHTLY_SRC} && "
        f"git -C {NIGHTLY_SRC} checkout -q {head_sha}; }} || exit {golden.WSL_INFRA_EXIT}; "
        f"{bundles_py('nightly-release', work)} --src {NIGHTLY_SRC} --sha {head_sha} "
        f"--name {pair.next} --version {pair.releases[pair.next]} --base {pair.base} "
        f"--sim-out '{golden.wsl_path(out / 'simulator.tar')}'"
    )
    try:
        raw = (run_wsl or wsl)(script, timeout_s=NIGHTLY_TIMEOUT_S)
    finally:
        head.unlink(missing_ok=True)
    result = cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    if result.get("ok") is not True:
        tail = str(result.get("tail") or "")[-800:]
        raise HeadBuildError(f"HEAD {head_sha[:7]} build failed (rc {result.get('rc')}): {tail}")
    return cast(dict[str, object], result["record"])


def build_site_bundle(
    ctx: Context,
    gen: int,
    release: str,
    window_open_at: datetime,
    project_dir: str | None = None,
    *,
    work: str = WORK,
) -> Path:
    """`ctx.site`의 g`gen` 번들(`release`)을 만든다. `project_dir`(WSL 경로)가 없으면 fixture
    `acme` 트리를 싣는다. `work`는 WSL 기록·store 디렉터리(G2는 `NIGHTLY_WORK`)."""
    site = ctx.site.site_id
    out = ctx.work
    start = window_open_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (window_open_at + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    project = f" --project-dir '{project_dir}'" if project_dir else ""
    wsl(
        f"{bundles_py('site', work)} "
        f"--out '{golden.wsl_path(out)}' --gen {gen} --product {ctx.pair.releases[release]} "
        f"--window-start {start} --window-end {end} --site {site}{project}"
    )
    built = out / f"pureiron-{site}-g{gen}.pib"
    metrics = json.loads(built.with_name(built.stem + ".bundle-metrics.json").read_text("utf-8"))
    # 같은 gen의 다른 release(설치 g1=BASE, 시나리오 g1=NEXT)가 서로 덮어쓰지 않게
    # 이름에 release를 둔다.
    pib = built.replace(out / f"pureiron-{site}-g{gen}-{release}.pib")
    entry: dict[str, object] = {"gen": gen, "release": release, "file": pib.name, **metrics}
    ctx.bundles = [b for b in ctx.bundles if b.get("file") != pib.name] + [entry]
    ctx.save()
    return pib


def site_bundle(ctx: Context, gen: int, release: str) -> Path:
    return ctx.work / f"pureiron-{ctx.site.site_id}-g{gen}-{release}.pib"


def release_tree(ctx: Context, release: str) -> Path:
    """NEXT·FAULT 번들 트리(`bundles.release_tree`)를 이 run의 번들 디렉터리에 새로 만든다(Windows
    호스트). 번들은 WSL이 이 디렉터리(`golden.wsl_path`)를 읽어 싣고, 판정 기대값(`tree_expect`)도
    같은 디렉터리에서 읽는다 — 실은 트리와 기대값이 어긋나지 않는다."""
    return bundles.release_tree(ctx.work / "trees" / release, release, ctx.pair)


def central(command: str, *args: str) -> dict[str, object]:
    """WSL `central.py <command>`의 JSON 한 줄(비밀값 없음 — 토큰은 `site-token`만 낸다)."""
    quoted = " ".join(shlex.quote(a) for a in args)
    raw = wsl(f"uv run --no-sync python {CENTRAL_PY} {command} {quoted}")
    return cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))


def site_token(site_id: str) -> str:
    """`central.py site-token`의 원문(파이프 전용 — 호출자가 ssh stdin으로만 넘긴다)."""
    return wsl(f"uv run --no-sync python {CENTRAL_PY} site-token --site {site_id}")


def wait_fleet(
    site_id: str,
    want: Callable[[dict[str, object]], bool],
    timeout_s: float = ONLINE_TIMEOUT_S,
) -> tuple[dict[str, object], float]:
    """`/fleet`(Caddy HTTPS, viewer 토큰) `site_id` 행이 `want`를 만족할 때까지. (마지막 행, 걸린
    초)."""
    started = time.monotonic()
    row: dict[str, object] = {}
    while time.monotonic() - started < timeout_s:
        try:
            row = judges.fleet_row(central("fleet", "--site", site_id), site_id)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            row = {}
        if row and want(row):
            break
        time.sleep(5)
    return row, round(time.monotonic() - started, 1)
