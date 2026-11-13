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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from harness import bundles, golden, judges, paths
from harness.context import E2EError, InfraError
from harness.release_pair import fault_record_problems

if TYPE_CHECKING:
    from harness.context import Context
    from harness.release_pair import ReleasePair

BUNDLES_PY = "tests/e2e/gates/harness/bundles.py"
CENTRAL_PY = "tests/e2e/gates/harness/central.py"
ONLINE_TIMEOUT_S = 180
INSTALL_TIMEOUT_S = 3600
NIGHTLY_TIMEOUT_S = 3600
"""HEAD 이미지 빌드(core·collector·web·simulator, BuildKit 캐시 있음) + 라벨 파생 + 저장."""
EXPECTED_SCHEMA_TIMEOUT_S = 1200
"""기대 스키마(`bundles.py expected-schema`) 전체 — DB 기동 180초 + migrate 600초 + 조회·정리."""
FAULTS_TIMEOUT_S = 1200
"""nightly FAULT 2종 파생(collector `sleep`·core migration 한 겹) + 저장(이미지 tar 6개)."""
GIT_TIMEOUT_S = 120
RECLAIM_TIMEOUT_S = 120
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


class SchemaBuildError(E2EError):
    """NEXT가 빈 DB에 끝까지 migrate되지 않는다 — 제품 결함(`product`, D9)이다."""


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
    실패했다(첫 실측). 최선 노력이다 — WSL 호출 실패·시간 초과도 예외 없이 넘긴다(효과는 부른 쪽이
    메모리를 다시 재서 본다, `preflight.reclaim_memory`)."""
    try:
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
            timeout=RECLAIM_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


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


def nightly_faults(
    pair: ReleasePair,
    next_record: dict[str, object],
    *,
    run_wsl: Callable[..., str] | None = None,
    work: str = NIGHTLY_WORK,
) -> dict[str, dict[str, object]]:
    """NEXT에서 파생한 FAULT 2종(`pair.fault` health·`pair.fault_migrate` migrate) 기록을 WSL
    nightly 작업 디렉터리에 만들고(`bundles.py nightly-faults`) 돌려준다. 기록마다
    `release_pair.fault_record_problems`로 다시 확인한다 — 어긋나면 `E2EError`(제품: 파생 규칙이
    깨지면 그 밤 시나리오가 무엇을 보는지 모른다). 스크립트 실패는 `golden.WslScriptError`(제품),
    WSL 호출 실패·Docker 무응답은 `golden.WslError`(인프라)."""
    script = f"{bundles_py('nightly-faults', work)} --next {pair.next}"
    raw = (run_wsl or wsl)(script, timeout_s=FAULTS_TIMEOUT_S)
    result = cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    records = cast(dict[str, dict[str, object]], result.get("records") or {})
    for name in (pair.fault, pair.fault_migrate):
        record = records.get(name, {})
        problems = fault_record_problems(pair, name, record, next_record)
        if problems:
            raise E2EError(f"{name}: " + "; ".join(problems))
    return {name: records[name] for name in (pair.fault, pair.fault_migrate)}


def expected_schema(
    out: Path,
    pair: ReleasePair,
    *,
    run_wsl: Callable[..., str] | None = None,
    work: str = NIGHTLY_WORK,
) -> dict[str, object]:
    """D9 기대 스키마(`bundles.py expected-schema` — WSL의 빈 TimescaleDB에 NEXT를 migrate하고
    probe와 같은 SQL로 읽은 이름 집합 5종·epoch)를 `out`(JSON)에 남기고 돌려준다. NEXT가 migrate되지
    않으면 `SchemaBuildError`(제품), 기대 epoch가 쌍의 NEXT epoch와 다르면 `E2EError`(제품),
    Docker 무응답은 `golden.WslError`(인프라)."""
    script = (
        f"{bundles_py('expected-schema', work)} --next {pair.next} --out '{golden.wsl_path(out)}'"
    )
    raw = (run_wsl or wsl)(script, timeout_s=EXPECTED_SCHEMA_TIMEOUT_S)
    result = cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    if result.get("ok") is not True:
        tail = str(result.get("tail") or "")[-800:]
        raise SchemaBuildError(
            f"{pair.next} does not migrate an empty DB ({result.get('stage')}, rc "
            f"{result.get('rc')}): {tail}"
        )
    schema = cast(dict[str, object], result["schema"])
    if schema.get("epoch") != str(pair.epochs[pair.next]):
        raise E2EError(f"expected schema epoch {schema.get('epoch')!r} != {pair.epochs[pair.next]}")
    return schema


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


REJECTS_TIMEOUT_S = 300
"""`bundles.py reject-materials`(키 2개·cert 하나·기록 하나·symlink) 전체."""


@dataclass(frozen=True)
class RejectMaterials:
    """G2-5 거부 재료(`bundles.py reject-materials`) — 값은 WSL 경로다."""

    release_key: str
    """`E_SIG`: root가 인증하지 않은 release key."""
    cert_dir: str
    """그 키에 다른 root가 발급한 `release-cert.json`·`.sig`."""
    root_pub: str
    """그 다른 root 공개키(빌더 자체 검증용)."""
    schema_version: str
    """`E_SCHEMA`: 가짜 기록의 달력 버전(`--product`)."""
    schema_epoch: int
    schema_releases: str
    schema_store: str

    def signing_options(self) -> tuple[str, ...]:
        return (
            "--release-key",
            self.release_key,
            "--cert-dir",
            self.cert_dir,
            "--root-pub",
            self.root_pub,
        )

    def schema_options(self) -> tuple[str, ...]:
        return ("--releases", self.schema_releases, "--store", self.schema_store)


def reject_materials(
    pair: ReleasePair, *, run_wsl: Callable[..., str] | None = None, work: str = NIGHTLY_WORK
) -> RejectMaterials:
    """WSL nightly 작업 디렉터리에 G2-5 거부 재료를 새로 만들고 돌려준다. 가짜 기록의 epoch가 NEXT +
    `bundles.SCHEMA_JUMP_EPOCHS`가 아니면 `E2EError`(제품: 그 재료로는 `E_SCHEMA`를 못 본다).
    스크립트 실패는 `golden.WslScriptError`(제품), WSL 호출 실패는 `golden.WslError`(인프라)."""
    script = f"{bundles_py('reject-materials', work)} --next {pair.next}"
    raw = (run_wsl or wsl)(script, timeout_s=REJECTS_TIMEOUT_S)
    found = cast(dict[str, dict[str, object]], json.loads(raw.strip().splitlines()[-1]))
    rogue, schema = found["rogue"], found["schema"]
    want = pair.epochs[pair.next] + bundles.SCHEMA_JUMP_EPOCHS
    if schema.get("epoch") != want:
        raise E2EError(f"schema-jump record epoch {schema.get('epoch')!r} != {want}")
    return RejectMaterials(
        release_key=str(rogue["release_key"]),
        cert_dir=str(rogue["cert_dir"]),
        root_pub=str(rogue["root_pub"]),
        schema_version=str(schema["version"]),
        schema_epoch=want,
        schema_releases=str(schema["releases"]),
        schema_store=str(schema["store"]),
    )


def build_variant(
    ctx: Context,
    gen: int,
    product: str,
    window_open_at: datetime,
    project_dir: str,
    *,
    label: str,
    site_id: str | None = None,
    options: tuple[str, ...] = (),
    work: str = NIGHTLY_WORK,
    run_wsl: Callable[..., str] | None = None,
) -> Path:
    """G2 번들 하나를 `ctx.work/variants`에 `pureiron-<site>-g<gen>-<label>.pib`로 만든다 — 달력
    버전 `product`, 트리 `project_dir`(WSL 경로), 현장 `site_id`(기본 `ctx.site`), 그 밖의
    `bundles.py site` 인자 `options`(서명 재료·`--min-updater`·기록 디렉터리). 거부 시험처럼 같은
    gen·release의 번들 여럿이 서로 덮어쓰지 않게 이름에 `label`을 둔다(`build_site_bundle`은
    그대로)."""
    site = site_id or ctx.site.site_id
    out = ctx.work / "variants"
    start = window_open_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (window_open_at + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra = "".join(f" {shlex.quote(option)}" for option in options)
    (run_wsl or wsl)(
        f"{bundles_py('site', work)} "
        f"--out '{golden.wsl_path(out)}' --gen {gen} --product {product} "
        f"--window-start {start} --window-end {end} --site {site} "
        f"--project-dir '{project_dir}'{extra}"
    )
    built = out / f"pureiron-{site}-g{gen}.pib"
    return built.replace(out / f"pureiron-{site}-g{gen}-{label}.pib")


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
