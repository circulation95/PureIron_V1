"""M2d 번들 재료(빌드 호스트 = WSL runner, 서명 키는 `~/pureiron/keys/test`).

    uv run --no-sync python tests/e2e/gates/harness/bundles.py fault-release --work W
    uv run --no-sync python tests/e2e/gates/harness/bundles.py site --work W --out O --gen 2 \\
        --product 2026.10.3-fault1 --window-start 2026-10-05T00:00:00Z --window-end ...
    uv run --no-sync python tests/e2e/gates/harness/bundles.py install --work W --out O \\
        --release 2026.10.2
    uv run --no-sync python tests/e2e/gates/harness/bundles.py m5-project --work W
    uv run --no-sync python tests/e2e/gates/harness/bundles.py absorb --work W --draft D \\
        --site-key E
    uv run --no-sync python tests/e2e/gates/harness/bundles.py nightly-release --work W \\
        --src S --sha <40자> --name n1a2b3c4 --version 2026.11.2-nightly.1a2b3c4 --base r5 \\
        --sim-out <simulator.tar>

release 쌍(`BASE`·`NEXT`·`FAULT`)은 `release_pair.py`가 정한다 — 함수는 `ReleasePair`를 인자로
받고, CLI는 `release_pair.DEFAULT`(M4 마감: r4→r5, fault r5f)를 쓴다.

- `fault-release`: `FAULT`(r5', `2026.11.1-fault1`) — `NEXT` 이미지 그대로에 collector만 세션을 맺지
  않는 파생 이미지(`sleep`). core는 healthy라 `up --wait`는 통과하고 `/readyz`(collector 세션)가
  실패해 `checking`에서 `E_HEALTH` → 롤백을 일으킨다. 저장소 `deploy/releases`는 건드리지 않고
  `W/releases`(저장소 release 기록 전부의 사본 + `FAULT`)와 `W/store`(`BASE`·`NEXT`는 실제 image
  store를 가리키는 symlink)에 둔다. 기록을 전부 옮기는 것은 `compat_app_min`이 저장소와 같게
  나오도록이다.
- `site`: fleet 현장 파일을 쓰고 `deploy/bundle/build_bundle.py`로 서명 번들을 만든다(release는
  `--releases W/releases`에서 `version == --product`로 찾는다). 프로젝트 트리는 `--project-dir`
  (기본 fixture `acme`).
- `m5-project`(M5 확인 ⓒ·ⓓ): `W/m5/repo` = fixture `acme` 사본에서 `project.yaml absorbs`를 뺀
  것 —
  "아직 absorb하지 않은 고객 repo". fixture는 `absorbs: {site-a: 3}`라 그대로 쓰면 VM의 현장
  site_rev(PATCH 2번 = 2)가 3 이하여서 드래프트 게이트가 통과하고(ⓒ가 `E_DRAFT`를 못 본다) absorb는
  리비전 퇴행(2 < 3) 충돌이 된다(ⓓ). 그 밖의 파일은 fixture와 바이트까지 같다.
- `release_tree`(함수 — `m2d/run.py`가 Windows 호스트에서 run 번들 디렉터리에 만들고 WSL 경로로
  `site --project-dir`에 넘긴다): NEXT·FAULT 번들 트리 = fixture 사본에서 태그
  `ReleasePair.tree_tag`의 description만 `tree_descriptions[release]`로 바꾼 것. c0(fixture)와
  다른 트리라 업그레이드·FAULT·재시도가 정의를 실제로 다시 쓴다(같은 트리는 core no-op).
- `absorb`(ⓓ): `W/m5/repo`를 `W/m5/absorbed`로 새로 복사하고 `pureiron-project absorb`(VM이
  서명한 draft.tar + VM `enrollment.json`) → `pureiron-project gate --draft`를 차례로 실행한다. 각
  종료 코드와 출력을 JSON 한 줄로 내고 판정은 `run.py`가 한다(실패해도 exit 0 — 결과에 증거를
  남기려고).

스크립트로 실행되므로(WSL) `tests/e2e/gates`를 `sys.path`에 넣고 `harness` 패키지로 import한다.
- `install`: `fetch_debs.sh` → `build_updater.sh` → `build_install_bundle.py`(실빌드, 네트워크
  필요). 설치 번들은 이미지를 싣지 않는다 — `--release`는 manifest `release`·파일 이름에만 쓰인다.
- `nightly-release`(G2 nightly NEXT, D7·D8): `--src`(HEAD를 checkout한 git 사본 — 빌드
  스크립트가 커밋을 읽는다)에서 `build_images.sh`로 core·collector·web·simulator를
  `<name>-src` 태그로 빌드하고, app 이미지는 `FROM <name>-src` + `LABEL pureiron.nightly=<sha>`
  파생으로 `<name>` 태그를 만든다(제품 Dockerfile·빌드 스크립트는 그대로 — 라벨이 config에 들어가
  코드 변경이 없는 날에도 BASE와 digest가 다르다). 기록은 `W/releases/<name>.json`, 이미지는
  `W/store/<name>/`(site-db는 HEAD lock이 BASE와 같으면 BASE tar를 가리킨다)에만 둔다 — 저장소
  `deploy/releases`·실제 image store는 건드리지 않는다. 시뮬레이터 tar는 `--sim-out`(G2가 VM에
  복사한다). nightly 기록·이미지는 최근 `NIGHTLY_KEEP`개만 남긴다. HEAD 빌드가 실패하면 exit 0으로
  `{"ok": false, ...}`를 낸다(제품 실패, D7) — 빌드 뒤 Docker가 응답하지 않으면 `INFRA_EXIT`(75,
  인프라 실패)이다. 그 밖의 0 아닌 종료(빌드가 성공한 뒤의 예외 포함)는 제품 스크립트 실패다.
  단계마다 시간 제한이 있다(`HEAD_BUILD_TIMEOUT_S`·`DOCKER_BUILD_TIMEOUT_S`·`DOCKER_SAVE_TIMEOUT_S`·
  `GIT_TIMEOUT_S`, M6-3b N3): 시간 초과 때 Docker가 답하면 제품(빌드 초과는 `{"ok": false, "rc":
  "timeout"}`, 그 뒤 단계는 exit 1), 답하지 않으면 인프라(75)다.
  prune은 NEXT와 그 FAULT 파생(`<NEXT>h`·`<NEXT>m`)을 한 묶음으로 남기고 지운다(store
  디렉터리·docker 태그).
- `nightly-faults`(G2 nightly FAULT 2종, M6-3 H9): 같은 run의 NEXT 기록에서 health
  fault(`<NEXT>h`, collector만 `sleep`)와 migrate fault(`<NEXT>m`, core에 실패하는 migration +
  epoch + 1)를 `W/releases`·`W/store`에만 만든다(`nightly_faults`).

    uv run --no-sync python tests/e2e/gates/harness/bundles.py nightly-faults --work W \\
        --next n1a2b3c4
- `expected-schema`(G2 스키마 자동 판정 D9, M6-3 H8): 임시 TimescaleDB(그 run의 site-db 이미지,
  호스트 포트 없음)에 NEXT core로 빈 DB를 끝까지 migrate하고 probe와 같은 SQL로 이름 집합
  5종·epoch를 `--out`에 쓴다(`expected_schema`). 컨테이너는 성공·실패 모두 지운다.

    uv run --no-sync python tests/e2e/gates/harness/bundles.py expected-schema --work W \\
        --next n1a2b3c4 --out <expected-schema.json>
- `reject-materials`(G2-5 거부, M6-3b J1): `W/rejects`에 `E_SIG` 재료(root가 인증하지 않은
  release key와 그 키를 인증한 다른 root·cert)와 `E_SCHEMA` 재료(NEXT 이미지 그대로에 스키마만
  NEXT epoch + 2인 가짜 기록·store)를 새로 만든다(`reject_materials`). `site`는 서명 재료
  (`--release-key`·`--cert-dir`·`--root-pub`)·`--min-updater`·기록
  디렉터리(`--releases`·`--store`)를 받는다 — 기본값은 그대로(시험 키·`--work`의 기록).

    uv run --no-sync python tests/e2e/gates/harness/bundles.py reject-materials --work W \\
        --next n1a2b3c4
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "deploy" / "build"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "site"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import probe
from images import config_digest_from_tar, config_digest_of_image, image_store, read_lock

from harness.release_pair import (
    DEFAULT,
    FAULT_CHANGED,
    FAULT_HEALTH,
    FAULT_MIGRATE,
    ReleasePair,
    fault_record_problems,
)
from pureiron_bundle import keys, trust
from pureiron_bundle.pib import CERT_FILE, CERT_SIG
from pureiron_core.platform.migrate import load_catalog
from pureiron_project import canonical
from pureiron_project.project import load_project

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = ROOT / "packages" / "project-tools" / "fixtures" / "acme"

KEYS = Path.home() / "pureiron" / "keys" / "test"
APP_SERVICES = ("core", "collector", "web")
FAULT_DOCKERFILE = """\
FROM pureiron/collector:{base}
# e2e fault: collector가 core에 gRPC 세션을 맺지 않는다 → core /readyz 503 → Updater E_HEALTH.
ENTRYPOINT ["sleep", "infinity"]
CMD []
"""


NIGHTLY_DOCKERFILE = """\
FROM pureiron/{svc}:{source}
LABEL pureiron.nightly={sha}
"""
NIGHTLY_BUILD = (*APP_SERVICES, "simulator")
NIGHTLY_KEEP = 3
"""WSL에 남기는 nightly 기록·이미지 수(디스크, 상위 계획 R8)."""
BUILD_TAIL_CHARS = 4000
DOCKER_INFO_TIMEOUT_S = 60
HEAD_BUILD_TIMEOUT_S = 40 * 60
"""`build_images.sh`(core·collector·web·simulator) 한 번 — BuildKit 캐시 적중이면 1분 안(M6-3a
실측 34초), 캐시가 없어도 의존성 설치 몇 분이다. 넘기면(Docker가 답하는데) HEAD가 빌드에서 멈춘
것으로 본다(제품)."""
DOCKER_BUILD_TIMEOUT_S = 5 * 60
"""라벨 파생·FAULT 파생 `docker build` 하나(`FROM` 한 겹 — 보통 몇 초)."""
DOCKER_SAVE_TIMEOUT_S = 10 * 60
"""`docker save` 하나(이미지 tar 수백 MB)."""
DOCKER_RM_TIMEOUT_S = 2 * 60
GIT_TIMEOUT_S = 60
INFRA_EXIT = 75
"""실행 환경 탓으로 멈출 때의 종료 코드(EX_TEMPFAIL) — Windows 쪽 `golden.wsl`이 `WslError`(infra)로
바꾼다(`golden.WSL_INFRA_EXIT`와 같다). 그 밖의 0 아닌 종료는 제품 스크립트 실패(product)다."""


def infra_exit(message: str) -> SystemExit:
    """`raise infra_exit(...)` — 메시지를 stderr에 쓰고 `INFRA_EXIT`로 끝내는 SystemExit."""
    print(message, file=sys.stderr, flush=True)
    return SystemExit(INFRA_EXIT)


def _run(*cmd: str, cwd: Path = ROOT, timeout_s: float | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout_s)


def local_digest(tag: str, scratch: Path) -> str | None:
    """로컬 docker 이미지 `tag`의 config digest(`docker save` 경유, `scratch`에 임시 tar). 없으면
    None."""
    inspect = subprocess.run(("docker", "image", "inspect", tag), capture_output=True, check=False)
    return config_digest_of_image(tag, scratch) if inspect.returncode == 0 else None


def ensure_image(
    tag: str,
    tar: Path,
    want: str,
    *,
    digest_of: Callable[[str], str | None],
    load: Callable[[Path], None],
) -> None:
    """로컬 `tag`의 config digest가 release 기록 값 `want`가 되게 한다. 없거나 다르면
    (같은 태그로 다시 빌드한 이미지) image store의 tar(`docker save`로 태그 포함)에서 적재하고
    다시 확인한다."""
    if digest_of(tag) == want:
        return
    load(tar)
    got = digest_of(tag)
    if got != want:
        raise SystemExit(f"{tag}: config digest {got} != release record {want} ({tar})")


def relink(link: Path, target: Path) -> None:
    """`link`를 `target`을 가리키는 symlink로 맞춘다. 끊겼거나 다른 곳(이전 쌍·옮긴 store)을
    가리키면 다시 건다 — `exists()`는 끊긴 symlink를 없다고 봐서 `symlink_to`가
    FileExistsError를 냈다."""
    if not target.exists():
        raise SystemExit(f"{target} missing")
    if link.is_symlink():
        if link.readlink() == target:
            return
        link.unlink()
    elif link.exists():
        raise SystemExit(f"{link} exists and is not a symlink")
    link.symlink_to(target, target_is_directory=target.is_dir())


def reset_records(releases: Path) -> None:
    """`releases`를 저장소 release 기록 전부의 사본으로 새로 맞춘다 — 이전 쌍의 사본·파생 기록이
    남지 않게, `compat_app_min`이 저장소와 같게 나오게."""
    releases.mkdir(parents=True, exist_ok=True)
    for old in releases.glob("*.json"):
        old.unlink()
    for record_path in sorted((ROOT / "deploy" / "releases").glob("*.json")):
        shutil.copy2(record_path, releases / record_path.name)


def fault_source(pair: ReleasePair) -> tuple[str, str, str]:
    """FAULT 기록의 (이름, 달력 버전, 파생 원본) — FAULT는 BASE가 아니라 NEXT에서 파생한다."""
    return pair.fault, pair.releases[pair.fault], pair.next


def fault_release(work: Path, pair: ReleasePair) -> Path:
    """`pair.fault` 기록·image store(`pair.next` 파생)를 `work`에 만든다."""
    fault_name, fault_version, fault_base = fault_source(pair)
    releases, store = work / "releases", work / "store"
    releases.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    real_store = image_store()
    for name in (pair.base, pair.next):
        if not (ROOT / "deploy" / "releases" / f"{name}.json").is_file():
            raise SystemExit(f"deploy/releases/{name}.json missing (release_record.py {name})")
        if not (real_store / name).is_dir():
            raise SystemExit(f"image store {real_store / name} missing")
    reset_records(releases)
    for name in (pair.base, pair.next):
        relink(store / name, real_store / name)
    base = json.loads((releases / f"{fault_base}.json").read_text(encoding="utf-8"))
    base_images = cast(dict[str, dict[str, str]], base["images"])
    fault_store = store / fault_name
    fault_store.mkdir(exist_ok=True)

    build_dir = work / "fault-collector"
    build_dir.mkdir(exist_ok=True)
    (build_dir / "Dockerfile").write_text(FAULT_DOCKERFILE.format(base=fault_base), "utf-8")
    # digest 계산용 임시 tar는 docker build context(build_dir) 밖에 둔다.
    scratch = work / "digest-scratch"
    for svc in APP_SERVICES:
        ensure_image(
            f"pureiron/{svc}:{fault_base}",
            real_store / fault_base / f"{svc}.tar",
            base_images[svc]["config_digest"],
            digest_of=lambda tag: local_digest(tag, scratch),
            load=lambda tar: _run("docker", "load", "-q", "-i", str(tar)),
        )
    _run("docker", "build", "-q", "-t", f"pureiron/collector:{fault_name}", str(build_dir))
    images: dict[str, dict[str, str]] = {}
    for svc in APP_SERVICES:
        tag = f"pureiron/{svc}:{fault_name}"
        if svc != "collector":
            _run("docker", "tag", f"pureiron/{svc}:{fault_base}", tag)
        tar = fault_store / f"{svc}.tar"
        _run("docker", "save", "-o", str(tar), tag)
        images[svc] = {"tag": tag, "config_digest": config_digest_from_tar(tar)}
        # core·web은 fault_base 이미지 그대로여야 한다(collector만 파생) — 롤백 판정이
        # 이 digest를 쓴다.
        want = base_images[svc]["config_digest"]
        if svc != "collector" and images[svc]["config_digest"] != want:
            raise SystemExit(f"{tag}: config digest {images[svc]['config_digest']} != {want}")
    relink(fault_store / "site-db.tar", real_store / fault_base / "site-db.tar")
    images["site-db"] = base_images["site-db"]
    record = {
        "name": fault_name,
        "release": fault_name,
        "version": fault_version,
        "commit": base["commit"],
        "images": images,
        "schema": base["schema"],
    }
    path = releases / f"{fault_name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


NIGHTLY_FAULT_NAME = re.compile(rf"^(n[0-9a-f]{{7}})[{FAULT_HEALTH}{FAULT_MIGRATE}]$")
"""nightly FAULT 이름(`n<sha7>h`·`n<sha7>m`) — 그 NEXT와 한 묶음으로 남기고 지운다."""


def nightly_group(name: str) -> str:
    """`name`이 속한 nightly 묶음(NEXT 이름) — FAULT 파생은 그 NEXT, 나머지는 자기 자신."""
    match = NIGHTLY_FAULT_NAME.fullmatch(name)
    return match[1] if match else name


def nightly_prune(names: dict[str, float], current: str, keep: int = NIGHTLY_KEEP) -> list[str]:
    """지울 nightly 이름: `names`(이름 → store 디렉터리 mtime)를 NEXT와 그 FAULT 파생(`h`·`m`)
    묶음으로 모아, `current` 묶음과 그 밖의 최근 `keep - 1`개 묶음(묶음 안 가장 새 mtime 기준)을 뺀
    나머지 묶음의 모든 이름(오래된 순)."""
    groups: dict[str, float] = {}
    for name, mtime in names.items():
        group = nightly_group(name)
        groups[group] = max(groups.get(group, mtime), mtime)
    others = sorted((g for g in groups if g != current), key=lambda g: groups[g], reverse=True)
    old = set(others[max(keep - 1, 0) :])
    return sorted((n for n in names if nightly_group(n) in old), key=lambda n: (names[n], n))


def _docker_ok() -> bool:
    try:
        done = subprocess.run(
            ("docker", "info"), capture_output=True, timeout=DOCKER_INFO_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def _save(tag: str, tar: Path) -> dict[str, str]:
    _run("docker", "save", "-o", str(tar), tag, timeout_s=DOCKER_SAVE_TIMEOUT_S)
    return {"tag": tag, "config_digest": config_digest_from_tar(tar)}


def nightly_images(
    store: Path, real_store: Path, src: Path, *, name: str, sha: str, base_record: dict[str, object]
) -> dict[str, dict[str, str]]:
    """`<name>-src`에서 라벨 파생 → `store/<name>/<svc>.tar`·config digest. site-db는 HEAD lock의
    upstream이 BASE 기록과 같으면 BASE tar를 가리키고 기록 값을 그대로 쓴다."""
    target = store / name
    target.mkdir(parents=True, exist_ok=True)
    images: dict[str, dict[str, str]] = {}
    for svc in APP_SERVICES:
        dockerfile = NIGHTLY_DOCKERFILE.format(svc=svc, source=f"{name}-src", sha=sha)
        tag = f"pureiron/{svc}:{name}"
        subprocess.run(
            ("docker", "build", "-q", "-t", tag, "-"),
            input=dockerfile,
            text=True,
            check=True,
            timeout=DOCKER_BUILD_TIMEOUT_S,
        )
        images[svc] = _save(tag, target / f"{svc}.tar")
    lock = read_lock(src / "deploy" / "releases" / "site-db.lock")
    upstream = f"{lock['upstream']}@{lock['digest']}"
    base_images = cast(dict[str, dict[str, str]], base_record["images"])
    base_db = base_images["site-db"]
    if base_db.get("upstream") == upstream:
        relink(target / "site-db.tar", real_store / str(base_record["name"]) / "site-db.tar")
        images["site-db"] = dict(base_db)
    else:
        images["site-db"] = {
            **_save(f"pureiron/site-db:{lock['version']}", target / "site-db.tar"),
            "upstream": upstream,
        }
    return images


def _git_head(src: Path) -> str:
    return subprocess.run(
        ("git", "-C", str(src), "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT_S,
    ).stdout.strip()


def _build_head(src: Path, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", "deploy/build/build_images.sh", *NIGHTLY_BUILD),
        cwd=src,
        env={**os.environ, "PUREIRON_RELEASE": f"{name}-src"},
        capture_output=True,
        text=True,
        check=False,
        timeout=HEAD_BUILD_TIMEOUT_S,
    )


def nightly_release(
    work: Path,
    src: Path,
    *,
    sha: str,
    name: str,
    version: str,
    base: str,
    sim_out: Path,
    git_head: Callable[[Path], str] = _git_head,
    build_head: Callable[[Path, str], subprocess.CompletedProcess[str]] = _build_head,
    docker_ok: Callable[[], bool] = _docker_ok,
    store_root: Callable[[], Path] = image_store,
    records: Path = ROOT / "deploy" / "releases",
    link: Callable[[Path, Path], None] = relink,
    finish: Callable[..., dict[str, dict[str, str]]] | None = None,
) -> dict[str, object]:
    """HEAD(`src`) 빌드 → 라벨 파생 → `W/releases/<name>.json`·`W/store/<name>/`·시뮬레이터 tar.
    빌드 실패는 `{"ok": false}`(제품 실패), 사본 커밋 불일치·빌드 뒤 Docker 무응답·BASE 기록/store
    없음은 `INFRA_EXIT`(인프라 실패), 빌드가 성공한 뒤의 다른 예외는 exit 1(제품 실패)이다. 시간
    초과(N3): git = 인프라(사본 문제), 빌드 = Docker가 답하면 `{"ok": false, "rc": "timeout"}`(제품
    — HEAD가 빌드에서 멈춤) 아니면 인프라, 빌드 뒤 파생·저장 = Docker가 답하면 exit 1(제품 스크립트
    실패, nightly-faults와 같은 규칙) 아니면 인프라. `git_head`·`build_head`·`docker_ok`·
    `store_root`·`records`·`link`·`finish`(빌드 뒤 단계 — 기본 `finish_release`)는 단위 시험이 바꿔
    끼운다."""
    try:
        head = git_head(src)
    except subprocess.TimeoutExpired:
        raise infra_exit(f"git rev-parse in {src} timed out after {GIT_TIMEOUT_S}s") from None
    if head != sha:
        raise infra_exit(f"{src} is at {head}, not {sha}")
    try:
        build = build_head(src, name)
    except subprocess.TimeoutExpired:
        if not docker_ok():
            raise infra_exit("docker is not responding after the HEAD build timed out") from None
        tail = f"build_images.sh did not finish in {HEAD_BUILD_TIMEOUT_S}s (docker answers)"
        return {"ok": False, "stage": "build", "rc": "timeout", "tail": tail}
    if build.returncode != 0:
        if not docker_ok():
            raise infra_exit("docker is not responding after the HEAD build failed")
        tail = (build.stdout + build.stderr)[-BUILD_TAIL_CHARS:]
        return {"ok": False, "stage": "build", "rc": build.returncode, "tail": tail}
    releases, store, real_store = work / "releases", work / "store", store_root()
    base_path = records / f"{base}.json"
    if not base_path.is_file() or not (real_store / base).is_dir():
        raise infra_exit(f"BASE {base}: record or image store {real_store / base} missing")
    reset_records(releases)
    store.mkdir(parents=True, exist_ok=True)
    link(store / base, real_store / base)
    base_record = cast(dict[str, object], json.loads(base_path.read_text(encoding="utf-8")))
    try:
        images = (finish or finish_release)(
            store, real_store, src, name=name, sha=sha, base_record=base_record, sim_out=sim_out
        )
    except subprocess.TimeoutExpired as exc:
        if not docker_ok():
            raise infra_exit(f"docker is not responding after {exc.cmd} timed out") from None
        raise SystemExit(f"{exc.cmd} timed out after {exc.timeout}s (docker answers)") from None
    catalog = load_catalog(src / "services" / "core" / "migrations")
    record = {
        "name": name,
        "release": name,
        "version": version,
        "commit": sha,
        "images": images,
        "schema": {
            "epoch": catalog.app_expected,
            "app_expected": catalog.app_expected,
            "app_max_compatible": catalog.app_max_compatible,
        },
    }
    path = releases / f"{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "name": name, "record": record, "simulator_tar": str(sim_out)}


def finish_release(
    store: Path,
    real_store: Path,
    src: Path,
    *,
    name: str,
    sha: str,
    base_record: dict[str, object],
    sim_out: Path,
) -> dict[str, dict[str, str]]:
    """HEAD 빌드 뒤 단계: 라벨 파생·이미지 저장(`nightly_images`) → 시뮬레이터 tar → 오래된 nightly
    묶음 prune(이미지 지우기는 최선 노력 — 시간 초과도 넘긴다). 이미지 기록을 돌려준다."""
    images = nightly_images(store, real_store, src, name=name, sha=sha, base_record=base_record)
    sim_out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        "docker",
        "save",
        "-o",
        str(sim_out),
        f"pureiron/simulator:{name}-src",
        timeout_s=DOCKER_SAVE_TIMEOUT_S,
    )
    nightly = {d.name: d.stat().st_mtime for d in store.glob("n*") if d.is_dir()}
    for old in nightly_prune(nightly, name):
        shutil.rmtree(store / old)
        for svc in NIGHTLY_BUILD:
            for tag in (f"pureiron/{svc}:{old}", f"pureiron/{svc}:{old}-src"):
                try:
                    subprocess.run(
                        ("docker", "image", "rm", tag),
                        capture_output=True,
                        check=False,
                        timeout=DOCKER_RM_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired:
                    continue  # 디스크 정리일 뿐이다 — 다음 run의 prune이 다시 지운다
    return images


FAULT_MIGRATE_SQL = """\
-- e2e fault(G2-6 ②, M6-3 H9): 표지 테이블을 만든 뒤 0으로 나눠 실패한다. 트랜잭션 migration이라
-- 실패하면 표지 테이블도 남지 않아야 한다 — 남으면 부분 적용이다.
CREATE TABLE e2e_fault_marker (id integer);
SELECT 1 / 0;
"""
FAULT_MIGRATE_DOCKERFILE = """\
FROM pureiron/core:{source}
# e2e fault: NEXT migration + 실패하는 migration 하나 + epochs.yaml에 NEXT epoch + 1
# -> Updater migrating에서 E_MIGRATE -> 롤백.
COPY migrations/ /opt/pureiron/migrations/
"""
CORE_MIGRATIONS = "/opt/pureiron/migrations"
"""core 이미지 안 migration 디렉터리(`services/core/Dockerfile` `PUREIRON_MIGRATIONS_DIR`)."""
FAULT_MIGRATION_NAME = "e2e_fault_marker"


def fault_migration(migrations: Path) -> tuple[str, int, str]:
    """`migrations`(NEXT core 이미지의 migration 디렉터리) 다음 migration의 (파일 이름, epoch, 새
    `epochs.yaml` 본문): 번호 = 가장 큰 번호 + 1(`NNNN_e2e_fault_marker.sql` — `load_catalog`의
    `NNNN_lower_snake.sql`), epoch = NEXT의 `app_expected` + 1, `epochs.yaml`은 끝에 그 epoch 한
    줄을 더한다. NEXT epoch를 상수로 두지 않는다 — HEAD가 migration을 더하면 같이 오른다."""
    catalog = load_catalog(migrations)
    version = max(m.version for m in catalog.migrations) + 1
    filename = f"{version:04d}_{FAULT_MIGRATION_NAME}.sql"
    epoch = catalog.app_expected + 1
    epochs = (migrations / "epochs.yaml").read_text(encoding="utf-8").rstrip("\n")
    return filename, epoch, f"{epochs}\n  {epoch}: {filename}\n"


def write_fault_migrations(source: Path, dest: Path) -> int:
    """`source`(NEXT migration) 사본 + 실패하는 migration + 새 `epochs.yaml`을 `dest`에 새로 만들고
    `load_catalog`로 다시 읽어 epoch가 NEXT + 1이고 마지막 파일이 새 migration인지 확인한다. 새
    epoch."""
    filename, epoch, epochs = fault_migration(source)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    (dest / filename).write_text(FAULT_MIGRATE_SQL, encoding="utf-8", newline="\n")
    (dest / "epochs.yaml").write_text(epochs, encoding="utf-8", newline="\n")
    catalog = load_catalog(dest)
    if catalog.app_expected != epoch or catalog.migrations[-1].filename != filename:
        raise SystemExit(f"{dest}: fault migration not accepted ({catalog.app_expected}, {epoch})")
    return epoch


def _image_migrations(tag: str, dest: Path) -> Path:
    """이미지 `tag` 안의 migration 디렉터리를 `dest`로 꺼낸다(`docker create` → `docker cp`)."""
    name = f"e2e-migrations-{os.getpid()}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run("docker", "create", "--name", name, tag)
    try:
        _run("docker", "cp", f"{name}:{CORE_MIGRATIONS}", str(dest))
    finally:
        subprocess.run(("docker", "rm", "-f", name), capture_output=True, check=False)
    return dest


def fault_pair(next_record: dict[str, object], health: str, migrate: str) -> ReleasePair:
    """WSL 쪽 FAULT 빌드가 쓰는 최소 쌍 — NEXT 기록(이름·버전·epoch)에서 FAULT 2종의 이름·버전
    (`<NEXT 버전>.fh`·`.fm`)·epoch(NEXT·NEXT + 1)를 정한다(`release_pair.nightly`와 같은 규칙)."""
    name, version = str(next_record["name"]), str(next_record["version"])
    epoch = cast(dict[str, int], next_record["schema"])["epoch"]
    empty: frozenset[str] = frozenset()
    return ReleasePair(
        base="",
        next=name,
        fault=health,
        fault_migrate=migrate,
        releases={
            name: version,
            health: f"{version}.f{FAULT_HEALTH}",
            migrate: f"{version}.f{FAULT_MIGRATE}",
        },
        epochs={name: epoch, health: epoch, migrate: epoch + 1},
        caggs=empty,
        base_columns=empty,
        base_tables=empty,
        base_indexes=empty,
        next_tables=empty,
        next_indexes=empty,
        tree_tag="",
        tree_descriptions={},
    )


def nightly_faults(
    work: Path,
    next_name: str,
    *,
    image_migrations: Callable[[str, Path], Path] = _image_migrations,
    docker_ok: Callable[[], bool] = _docker_ok,
) -> dict[str, object]:
    """`build_faults`를 돌린다. docker 명령(`build`·`save`·`tag`·`load`)이 실패했을 때 Docker가
    응답하지 않으면 `INFRA_EXIT`(인프라 — `nightly-release`와 같은 규칙, M6-3a 리뷰 M2), 응답하면
    그대로 다시 던진다(제품 스크립트 실패, exit 1)."""
    try:
        return build_faults(work, next_name, image_migrations=image_migrations)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if not docker_ok():
            raise infra_exit(f"docker is not responding after {exc.cmd} failed") from None
        raise


def build_faults(
    work: Path,
    next_name: str,
    *,
    image_migrations: Callable[[str, Path], Path] = _image_migrations,
) -> dict[str, object]:
    """nightly FAULT 2종(M6-3 H9) `<NEXT>h`·`<NEXT>m`을 `W/releases/<이름>.json`·
    `W/store/<이름>/`에만 만든다(저장소 `deploy/releases`·실제 image store는 건드리지 않는다). 둘 다
    `W/releases/<NEXT>.json`(같은 run의 `nightly-release`)에서 파생한다:

    - health(`h`): collector만 `sleep`(FAULT_DOCKERFILE — `/readyz` 503 → `E_HEALTH`), core·web은
      NEXT 이미지 그대로(digest 같음), 스키마 = NEXT.
    - migrate(`m`): core = `FROM pureiron/core:<NEXT>` + NEXT 이미지의 migration 사본에 실패하는
      migration 하나와 epoch + 1(`write_fault_migrations`), collector·web은 NEXT 그대로. 스키마
      epoch·app_expected = NEXT + 1, app_max_compatible = NEXT + 2.

    site-db는 NEXT의 tar를 가리킨다. 기록마다 `fault_record_problems`가 비어야 한다(아니면 exit 1 —
    제품 스크립트 실패). `{"ok": true, "records": {이름: 기록}}`."""
    releases, store = work / "releases", work / "store"
    next_path = releases / f"{next_name}.json"
    if not next_path.is_file():
        raise SystemExit(f"{next_path} missing (bundles.py nightly-release)")
    next_record = cast(dict[str, object], json.loads(next_path.read_text(encoding="utf-8")))
    pair = fault_pair(next_record, next_name + FAULT_HEALTH, next_name + FAULT_MIGRATE)
    next_images = cast(dict[str, dict[str, str]], next_record["images"])
    scratch = work / "digest-scratch"
    for svc in APP_SERVICES:
        ensure_image(
            f"pureiron/{svc}:{next_name}",
            store / next_name / f"{svc}.tar",
            next_images[svc]["config_digest"],
            digest_of=lambda tag: local_digest(tag, scratch),
            load=lambda tar: _run("docker", "load", "-q", "-i", str(tar)),
        )
    build_root = work / "fault-build"
    if build_root.exists():
        shutil.rmtree(build_root)
    records: dict[str, object] = {}
    for name, kind in ((pair.fault, FAULT_HEALTH), (pair.fault_migrate, FAULT_MIGRATE)):
        target = store / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        build_dir = build_root / name
        build_dir.mkdir(parents=True)
        changed = FAULT_CHANGED[kind]
        epoch = pair.epochs[next_name]
        if kind == FAULT_HEALTH:
            dockerfile = FAULT_DOCKERFILE.format(base=next_name)
        else:
            source = image_migrations(f"pureiron/core:{next_name}", build_root / "next-migrations")
            if load_catalog(source).app_expected != epoch:
                raise SystemExit(f"pureiron/core:{next_name} migrations are not epoch {epoch}")
            epoch = write_fault_migrations(source, build_dir / "migrations")
            dockerfile = FAULT_MIGRATE_DOCKERFILE.format(source=next_name)
        (build_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        _run(
            "docker",
            "build",
            "-q",
            "-t",
            f"pureiron/{changed}:{name}",
            str(build_dir),
            timeout_s=DOCKER_BUILD_TIMEOUT_S,
        )
        images: dict[str, dict[str, str]] = {}
        for svc in APP_SERVICES:
            tag = f"pureiron/{svc}:{name}"
            if svc != changed:
                _run("docker", "tag", f"pureiron/{svc}:{next_name}", tag)
            images[svc] = _save(tag, target / f"{svc}.tar")
        relink(target / "site-db.tar", (store / next_name / "site-db.tar").resolve())
        images["site-db"] = dict(next_images["site-db"])
        record: dict[str, object] = {
            "name": name,
            "release": name,
            "version": pair.releases[name],
            "commit": next_record["commit"],
            "images": images,
            "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
        }
        problems = fault_record_problems(pair, name, record, next_record)
        if problems:
            raise SystemExit(f"{name}: " + "; ".join(problems))
        path = releases / f"{name}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        records[name] = record
    shutil.rmtree(build_root)
    return {"ok": True, "records": records}


SCHEMA_LABEL = "pureiron.e2e=expected-schema"
"""기대 스키마용 임시 컨테이너·네트워크 표지 — 시작할 때 남은 것을 이것으로 찾아 지운다."""
SCHEMA_DB_READY_S = 180
"""임시 TimescaleDB가 TCP로 받을 때까지(initdb 포함)."""
SCHEMA_MIGRATE_S = 600
"""빈 DB에 NEXT를 끝까지 migrate하는 시간."""
SCHEMA_QUERY_S = 60
SCHEMA_POLL_S = 2
SCHEMA_DOCKER_S = 120
"""그 밖의 docker 명령 하나(network·run -d·rm·load)."""
EXPECTED_KEYS = ("epoch", *probe.SCHEMA_KEYS)
"""기대 스키마 항목 — probe와 같은 SQL(`probe.DB_SQL`)로 읽는다."""

DockerRun = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _docker(args: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", *args), capture_output=True, text=True, timeout=timeout_s, check=False
    )


OWNER_PID = re.compile(r"-(\d+)$")
"""임시 컨테이너·네트워크 이름 끝의 만든 프로세스 pid(`e2e-schema-db-<NEXT>-<pid>`)."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 다른 사용자의 살아 있는 프로세스
    return True


def stale_names(names: Sequence[str], alive: Callable[[int], bool]) -> list[str]:
    """`names` 중 만든 프로세스(이름 끝 pid)가 이미 끝난 것 — 지금 도는 다른 expected-schema(같은
    PC의
    수동 WSL 빌드)의 것은 남긴다(3a 리뷰 L4). pid가 없는 이름은 누구 것인지 몰라 남긴다."""
    stale: list[str] = []
    for name in names:
        found = OWNER_PID.search(name)
        if found is not None and not alive(int(found[1])):
            stale.append(name)
    return stale


def _remove_labeled(docker: DockerRun, alive: Callable[[int], bool]) -> None:
    """`SCHEMA_LABEL`이 붙은 컨테이너·네트워크 중 만든 프로세스가 끝난 것(이전 실행이 시간 초과로
    죽어
    남긴 것)만 지운다."""
    label = ("--filter", f"label={SCHEMA_LABEL}")
    found = docker(("ps", "-a", *label, "--format", "{{.Names}}"), SCHEMA_DOCKER_S)
    stale = stale_names(found.stdout.split(), alive)
    if stale:
        docker(("rm", "-f", *stale), SCHEMA_DOCKER_S)
    found = docker(("network", "ls", *label, "--format", "{{.Name}}"), SCHEMA_DOCKER_S)
    stale = stale_names(found.stdout.split(), alive)
    if stale:
        docker(("network", "rm", *stale), SCHEMA_DOCKER_S)


@dataclass(frozen=True)
class SchemaDb:
    """기대 스키마용 임시 DB — 이름·네트워크·이미지와 docker·시계."""

    name: str
    net: str
    image: str
    docker: DockerRun
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    migrator: str = ""
    """migrate 클라이언트 컨테이너 이름 — 시간 초과로 클라이언트만 죽어도 `remove`가 지운다(L4)."""

    def start(self) -> None:
        """네트워크 → TimescaleDB(신뢰 인증, 호스트 포트 없음) → TCP로 받을 때까지. 실패는
        인프라."""
        label = ("--label", SCHEMA_LABEL)
        if self.docker(("network", "create", *label, self.net), SCHEMA_DOCKER_S).returncode:
            raise infra_exit(f"docker network create {self.net} failed")
        env = ("POSTGRES_USER=pureiron", "POSTGRES_DB=pureiron", "POSTGRES_HOST_AUTH_METHOD=trust")
        run = ("run", "-d", "--name", self.name, *label, "--network", self.net)
        started = self.docker(
            (
                *run,
                *(a for e in env for a in ("-e", e)),
                self.image,
                *("postgres", "-c", "timescaledb.telemetry_level=off"),
            ),
            SCHEMA_DOCKER_S,
        )
        if started.returncode:
            raise infra_exit(f"site-db {self.image} did not start: {started.stderr[-800:]}")
        # TCP로 본다 — initdb 임시 서버는 unix socket만 열고 곧 재시작한다(compose healthcheck)
        ready = ("exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U", "pureiron")
        deadline = self.clock() + SCHEMA_DB_READY_S
        while self.docker((*ready, "-d", "pureiron"), SCHEMA_QUERY_S).returncode:
            if self.clock() >= deadline:
                raise infra_exit(f"site-db {self.image} not ready in {SCHEMA_DB_READY_S}s")
            self.sleep(SCHEMA_POLL_S)

    def migrate(self, core: str) -> subprocess.CompletedProcess[str] | None:
        """`core` 이미지로 빈 DB를 끝까지 migrate한다(시간 초과면 None)."""
        dsn = f"PUREIRON_DB_DSN=postgresql://pureiron@{self.name}:5432/pureiron"
        named = ("--name", self.migrator) if self.migrator else ()
        cmd = (
            *("run", "--rm", *named, "--label", SCHEMA_LABEL, "--network", self.net),
            *("-e", dsn, core),
        )
        try:
            return self.docker((*cmd, "migrate", "up"), SCHEMA_MIGRATE_S)
        except subprocess.TimeoutExpired:
            return None

    def read(self) -> dict[str, object]:
        """probe와 같은 SQL로 `EXPECTED_KEYS`를 읽는다. psql 실패는 하네스 스크립트 실패(exit 1)."""
        schema: dict[str, object] = {}
        for key in EXPECTED_KEYS:
            psql = ("exec", self.name, "psql", "-At", "-U", "pureiron", "-d", "pureiron", "-c")
            done = self.docker((*psql, probe.DB_SQL[key]), SCHEMA_QUERY_S)
            if done.returncode:
                raise SystemExit(f"{key}: psql exited {done.returncode}: {done.stderr[-800:]}")
            schema[key] = done.stdout.strip()
        return schema

    def remove(self) -> None:
        containers = (self.name, self.migrator) if self.migrator else (self.name,)
        for cleanup in (("rm", "-f", *containers), ("network", "rm", self.net)):
            try:
                self.docker(cleanup, SCHEMA_DOCKER_S)
            except subprocess.TimeoutExpired:
                print(f"cleanup timed out: docker {' '.join(cleanup)}", file=sys.stderr)


def expected_schema(
    work: Path,
    next_name: str,
    out: Path,
    *,
    docker: DockerRun = _docker,
    docker_ok: Callable[[], bool] = _docker_ok,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    alive: Callable[[int], bool] = _pid_alive,
) -> dict[str, object]:
    """D9 기대 스키마(M6-3 H8): 그 run의 site-db 이미지로 임시 TimescaleDB를 띄우고(호스트 포트
    없음 — 같은 PC의 CI가 25432·25433을 쓴다) NEXT core 이미지로 빈 DB를 끝까지 migrate한 뒤, VM
    probe와 같은 SQL(`probe.DB_SQL`)로 이름 집합 5종과 epoch를 읽어 `out`(JSON)에 쓴다. 컨테이너·
    네트워크는 성공·실패 모두 지운다(시작할 때 남은 것도). 결과 한 줄: `{"ok": true, "schema":
    {...}}` / NEXT가 빈 DB에 migrate되지 않으면 `{"ok": false, "stage": "migrate", ...}`(제품,
    exit 0). Docker 무응답·DB가 끝내 받지 않음은 `INFRA_EXIT`(인프라)."""
    path = work / "releases" / f"{next_name}.json"
    images = cast(dict[str, dict[str, str]], json.loads(path.read_text("utf-8"))["images"])
    core, image = images["core"]["tag"], images["site-db"]["tag"]
    suffix = f"{next_name}-{os.getpid()}"
    db = SchemaDb(
        f"e2e-schema-db-{suffix}",
        f"e2e-schema-{suffix}",
        image,
        docker,
        sleep,
        clock,
        migrator=f"e2e-schema-migrate-{suffix}",
    )
    try:
        if docker(("image", "inspect", image), SCHEMA_DOCKER_S).returncode:
            tar = work / "store" / next_name / "site-db.tar"
            if docker(("load", "-q", "-i", str(tar)), SCHEMA_DOCKER_S).returncode:
                raise infra_exit(f"cannot load {image} from {tar}")
        _remove_labeled(docker, alive)
        db.start()
        migrated = db.migrate(core)
        if migrated is None or migrated.returncode:
            if not docker_ok():
                raise infra_exit("docker is not responding after migrate failed")
            rc = "timeout" if migrated is None else migrated.returncode
            tail = "" if migrated is None else (migrated.stdout + migrated.stderr)
            return {"ok": False, "stage": "migrate", "rc": rc, "tail": tail[-BUILD_TAIL_CHARS:]}
        schema = db.read()
    except subprocess.TimeoutExpired as exc:
        raise infra_exit(f"docker did not answer: {exc}") from None
    finally:
        db.remove()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "schema": schema, "path": str(out), "image": core}


@dataclass(frozen=True)
class Signing:
    """번들 서명 재료 — 기본은 시험 키(`KEYS`). G2-5 `E_SIG`는 root가 인증하지 않은 release key와
    그 키를 인증한 다른 root(`reject_materials`)를 넘긴다(빌더의 자체 검증은 그 root로 통과한다)."""

    release_key: Path = KEYS / "release.key"
    cert_dir: Path = KEYS
    root_pub: Path = KEYS / "test-root.pub"


def site(
    work: Path,
    out: Path,
    *,
    gen: int,
    product: str,
    start: str,
    end: str,
    site_id: str,
    project_dir: Path = FIXTURE,
    signing: Signing | None = None,
    min_updater: str | None = None,
    releases: Path | None = None,
    store: Path | None = None,
) -> Path:
    """fleet 현장 파일을 쓰고 `build_bundle.py`로 서명 번들을 만든다. 기본은 `work`의 기록·store와
    시험 키다. G2-5 거부 재료는 서명(`signing`)·`min_updater`·기록 디렉터리(`releases`·`store`)를
    바꾼다."""
    keys_used = signing or Signing()
    fleet = work / "sites" / f"{site_id}-g{gen}.yaml"
    fleet.parent.mkdir(parents=True, exist_ok=True)
    fleet.write_text(
        f"site: {site_id}\ngeneration: {gen}\nproduct: {product}\n"
        "project: { repo: pureiron-project-acme, version: v1 }\n"
        f'window: {{ once: {{ start: "{start}", end: "{end}" }} }}\n',
        encoding="utf-8",
    )
    out.mkdir(parents=True, exist_ok=True)
    extra = ("--min-updater", min_updater) if min_updater is not None else ()
    _run(
        "uv",
        "run",
        "--no-sync",
        "python",
        "deploy/bundle/build_bundle.py",
        "--fleet-file",
        str(fleet),
        "--project-dir",
        str(project_dir),
        "--release-key",
        str(keys_used.release_key),
        "--cert-dir",
        str(keys_used.cert_dir),
        "--root-pub",
        str(keys_used.root_pub),
        "--fleet-commit",
        "e2e0000",
        "--out",
        str(out),
        "--releases",
        str(releases or work / "releases"),
        "--store",
        str(store or work / "store"),
        *extra,
    )
    return out / f"pureiron-{site_id}-g{gen}.pib"


REJECTS = "rejects"
"""G2-5 거부 재료 디렉터리(`W/rejects`) — 부를 때마다 새로 만든다. nightly 작업 디렉터리에만
둔다."""
SCHEMA_JUMP = "s"
"""가짜 스키마 기록 이름 접미사(`<NEXT>s`) — FAULT 묶음 정규식(`h`·`m`) 밖이고 `W/rejects`에만
있다."""
SCHEMA_JUMP_EPOCHS = 2
"""가짜 기록의 epoch = NEXT + 2 — 현장(NEXT epoch)에서 두 epoch를 건너뛰므로 `E_SCHEMA`다(+ 1이면
MIGRATE로 받아들여진다)."""
ROGUE_CERT_DAYS = 30


def schema_jump_record(next_record: dict[str, object]) -> dict[str, object]:
    """`E_SCHEMA` 재료: NEXT 기록의 이미지 그대로(digest 검사를 통과한다)에 스키마만 NEXT epoch +
    `SCHEMA_JUMP_EPOCHS`인 가짜 release 기록. 버전 `<NEXT 버전>.fs`(fleet 버전 형식 안)."""
    epoch = cast(dict[str, int], next_record["schema"])["epoch"] + SCHEMA_JUMP_EPOCHS
    name = f"{next_record['name']}{SCHEMA_JUMP}"
    return {
        "name": name,
        "release": name,
        "version": f"{next_record['version']}.f{SCHEMA_JUMP}",
        "commit": next_record["commit"],
        "images": json.loads(json.dumps(next_record["images"])),
        "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
    }


def reject_materials(
    work: Path,
    next_name: str,
    *,
    now: datetime,
    link: Callable[[Path, Path], None] = relink,
) -> dict[str, object]:
    """G2-5 거부 재료를 `W/rejects`에 새로 만든다:

    - `E_SIG`(`rogue/`): 새 root key와 새 release key, 그 root가 release key에 발급한 cert
      (`not_before` = `now` - 1일). 현장의 진짜 root로는 cert 서명이 맞지 않는다 — payload digest는
      정상이다.
    - `E_SCHEMA`(`releases/`·`store/`): `schema_jump_record` 하나와 그 이미지(NEXT store tar를
    가리키는
      symlink).

    `E_UPDATER`(`--min-updater`)·`E_SITE`·`E_DIGEST`는 재료가 따로 없다. 결과 JSON(WSL 경로)."""
    root = work / REJECTS
    if root.exists():
        shutil.rmtree(root)
    rogue = root / "rogue"
    rogue.mkdir(parents=True)
    root_seed, release_seed = keys.generate_seed(), keys.generate_seed()
    keys.write_private_key(rogue / "release.key", release_seed)
    keys.write_public_key(rogue / "root.pub", keys.public_key(root_seed))
    cert, cert_sig = trust.issue_cert(
        root_seed,
        keys.public_key(release_seed),
        1,
        now - timedelta(days=1),
        days=ROGUE_CERT_DAYS,
    )
    (rogue / CERT_FILE).write_bytes(cert)
    (rogue / CERT_SIG).write_bytes(cert_sig)
    next_path = work / "releases" / f"{next_name}.json"
    if not next_path.is_file():
        raise SystemExit(f"{next_path} missing (bundles.py nightly-release)")
    record = schema_jump_record(cast(dict[str, object], json.loads(next_path.read_text("utf-8"))))
    releases, store = root / "releases", root / "store" / str(record["name"])
    releases.mkdir(parents=True)
    store.mkdir(parents=True)
    (releases / f"{record['name']}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for svc in (*APP_SERVICES, "site-db"):
        link(store / f"{svc}.tar", (work / "store" / next_name / f"{svc}.tar").resolve())
    schema = cast(dict[str, int], record["schema"])
    return {
        "ok": True,
        "rogue": {
            "release_key": str(rogue / "release.key"),
            "cert_dir": str(rogue),
            "root_pub": str(rogue / "root.pub"),
        },
        "schema": {
            "version": record["version"],
            "epoch": schema["epoch"],
            "releases": str(releases),
            "store": str(root / "store"),
        },
    }


def unabsorbed_copy(source: Path, dest: Path) -> Path:
    """`source` 트리를 `dest`에 새로 복사하고 `project.yaml`에서 `absorbs`만 뺀다(정규형으로 다시
    쓴다). 결과가 유효한 프로젝트가 아니면 멈춘다."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    meta = dest / "project.yaml"
    doc = cast(dict[str, canonical.Json], canonical.load(meta.read_text("utf-8"), "project.yaml"))
    doc.pop("absorbs", None)
    meta.write_text(canonical.dump(doc), encoding="utf-8", newline="\n")
    project = load_project(dest)
    if not project.ok:
        raise SystemExit(f"{dest}: " + "; ".join(project.problems))
    return dest


def m5_project(work: Path) -> Path:
    return unabsorbed_copy(FIXTURE, work / "m5" / "repo")


def release_tree(dest: Path, release: str, pair: ReleasePair, source: Path = FIXTURE) -> Path:
    """`source` 트리를 `dest`에 새로 복사하고 태그 `pair.tree_tag`의 `description`만
    `pair.tree_descriptions[release]`로 바꾼다(그 태그 파일을 정규형으로 다시 쓴다). 결과가 유효한
    프로젝트가 아니거나 태그가 정확히 하나가 아니면 멈춘다."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    found: list[tuple[Path, list[dict[str, canonical.Json]]]] = []
    for path in sorted((dest / "tags").glob("*.yaml")):
        items = cast(list[dict[str, canonical.Json]], canonical.load(path.read_text("utf-8")))
        for item in items:
            if item.get("path") == pair.tree_tag:
                item["description"] = pair.tree_descriptions[release]
                found.append((path, items))
    if len(found) != 1:
        raise SystemExit(f"{source}: tag {pair.tree_tag} found {len(found)} times (want 1)")
    path, items = found[0]
    path.write_text(canonical.dump(cast(canonical.Json, items)), encoding="utf-8", newline="\n")
    project = load_project(dest)
    if not project.ok:
        raise SystemExit(f"{dest}: " + "; ".join(project.problems))
    return dest


def _tool(*cmd: str) -> dict[str, object]:
    done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    return {"rc": done.returncode, "stdout": done.stdout[-2000:], "stderr": done.stderr[-2000:]}


def absorb(
    work: Path,
    draft: Path,
    site_key: Path,
    site_id: str,
    *,
    repo: Path | None = None,
    dest: Path | None = None,
) -> dict[str, object]:
    """ⓓ: 아직 absorb하지 않은 repo 사본에 현장 드래프트를 회수하고 게이트를 사전 점검한다. 기본
    repo·결과는 `W/m5/repo`·`W/m5/absorbed`(run.py). G2-8은 G2 트리(`--repo`)를 `--dest`에 복사해
    회수한다."""
    repo = repo or work / "m5" / "repo"
    absorbed = dest or work / "m5" / "absorbed"
    if not repo.is_dir():
        raise SystemExit(f"{repo} missing (bundles.py m5-project)")
    if absorbed.exists():
        shutil.rmtree(absorbed)
    shutil.copytree(repo, absorbed)
    project = ("uv", "run", "--no-sync", "pureiron-project")
    key = ("--site", site_id, "--draft", str(draft), "--site-key", str(site_key))
    result: dict[str, object] = {
        "dir": str(absorbed),
        "absorb": _tool(*project, "absorb", *key, str(absorbed)),
    }
    if cast(dict[str, object], result["absorb"])["rc"] == 0:
        result["gate"] = _tool(*project, "gate", str(absorbed), *key)
    return result


def install(work: Path, out: Path, release: str) -> Path:
    debs, updater = work / "debs", work / "updater"
    if not (debs / "debs.json").exists():
        _run("bash", "deploy/install/debs/fetch_debs.sh", str(debs))
    shutil.rmtree(updater, ignore_errors=True)
    _run("bash", "deploy/build/build_updater.sh", str(updater))
    (tar,) = sorted(updater.glob("updater-*.tar.gz"))
    _run(
        "uv",
        "run",
        "--no-sync",
        "python",
        "deploy/bundle/build_install_bundle.py",
        "--release",
        release,
        "--updater-tar",
        str(tar),
        "--debs-dir",
        str(debs),
        "--release-key",
        str(KEYS / "release.key"),
        "--cert-dir",
        str(KEYS),
        "--root-pub",
        str(KEYS / "test-root.pub"),
        "--out",
        str(out),
    )
    return out / f"pureiron-install-{release}.pib"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    fault = sub.add_parser("fault-release")
    fault.add_argument("--work", type=Path, required=True)
    site_cmd = sub.add_parser("site")
    site_cmd.add_argument("--work", type=Path, required=True)
    site_cmd.add_argument("--out", type=Path, required=True)
    site_cmd.add_argument("--gen", type=int, required=True)
    site_cmd.add_argument("--product", required=True)
    site_cmd.add_argument("--window-start", required=True)
    site_cmd.add_argument("--window-end", required=True)
    site_cmd.add_argument("--site", default="site-a")
    site_cmd.add_argument("--project-dir", type=Path, default=FIXTURE)
    site_cmd.add_argument("--release-key", type=Path, default=None, help="기본: 시험 키")
    site_cmd.add_argument("--cert-dir", type=Path, default=None, help="기본: 시험 키 디렉터리")
    site_cmd.add_argument("--root-pub", type=Path, default=None, help="빌더 자체 검증용 root")
    site_cmd.add_argument("--min-updater", default=None, help="manifest min_updater")
    site_cmd.add_argument("--releases", type=Path, default=None, help="기본: --work/releases")
    site_cmd.add_argument("--store", type=Path, default=None, help="기본: --work/store")
    rejects_cmd = sub.add_parser("reject-materials")
    rejects_cmd.add_argument("--work", type=Path, required=True)
    rejects_cmd.add_argument("--next", required=True, help="NEXT 이름 n<sha7>")
    m5 = sub.add_parser("m5-project")
    m5.add_argument("--work", type=Path, required=True)
    absorb_cmd = sub.add_parser("absorb")
    absorb_cmd.add_argument("--work", type=Path, required=True)
    absorb_cmd.add_argument("--draft", type=Path, required=True)
    absorb_cmd.add_argument("--site-key", type=Path, required=True)
    absorb_cmd.add_argument("--site", default="site-a")
    absorb_cmd.add_argument("--repo", type=Path, default=None, help="기본: --work/m5/repo")
    absorb_cmd.add_argument("--dest", type=Path, default=None, help="기본: --work/m5/absorbed")
    install_cmd = sub.add_parser("install")
    install_cmd.add_argument("--work", type=Path, required=True)
    install_cmd.add_argument("--out", type=Path, required=True)
    install_cmd.add_argument("--release", required=True)
    nightly_cmd = sub.add_parser("nightly-release")
    nightly_cmd.add_argument("--work", type=Path, required=True)
    nightly_cmd.add_argument("--src", type=Path, required=True)
    nightly_cmd.add_argument("--sha", required=True)
    nightly_cmd.add_argument("--name", required=True)
    nightly_cmd.add_argument("--version", required=True)
    nightly_cmd.add_argument("--base", required=True)
    nightly_cmd.add_argument("--sim-out", type=Path, required=True)
    faults_cmd = sub.add_parser("nightly-faults")
    faults_cmd.add_argument("--work", type=Path, required=True)
    faults_cmd.add_argument("--next", required=True, help="NEXT 이름 n<sha7>")
    schema_cmd = sub.add_parser("expected-schema")
    schema_cmd.add_argument("--work", type=Path, required=True)
    schema_cmd.add_argument("--next", required=True, help="NEXT 이름 n<sha7>")
    schema_cmd.add_argument("--out", type=Path, required=True, help="expected-schema.json")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.command == "fault-release":
        print(fault_release(args.work, DEFAULT))
    elif args.command == "site":
        default = Signing()
        signing = Signing(
            args.release_key or default.release_key,
            args.cert_dir or default.cert_dir,
            args.root_pub or default.root_pub,
        )
        print(
            site(
                args.work,
                args.out,
                gen=args.gen,
                product=args.product,
                start=args.window_start,
                end=args.window_end,
                site_id=args.site,
                project_dir=args.project_dir,
                signing=signing,
                min_updater=args.min_updater,
                releases=args.releases,
                store=args.store,
            )
        )
    elif args.command == "reject-materials":
        found = reject_materials(args.work, args.next, now=datetime.now(UTC))
        print(json.dumps(found, sort_keys=True))
    elif args.command == "m5-project":
        print(m5_project(args.work))
    elif args.command == "absorb":
        absorbed = absorb(
            args.work, args.draft, args.site_key, args.site, repo=args.repo, dest=args.dest
        )
        print(json.dumps(absorbed))
    elif args.command == "nightly-release":
        result = nightly_release(
            args.work,
            args.src,
            sha=args.sha,
            name=args.name,
            version=args.version,
            base=args.base,
            sim_out=args.sim_out,
        )
        print(json.dumps(result, sort_keys=True))
    elif args.command == "nightly-faults":
        print(json.dumps(nightly_faults(args.work, args.next), sort_keys=True))
    elif args.command == "expected-schema":
        print(json.dumps(expected_schema(args.work, args.next, args.out), sort_keys=True))
    else:
        print(install(args.work, args.out, args.release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
