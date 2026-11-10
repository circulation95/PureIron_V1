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
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "deploy" / "build"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from images import config_digest_from_tar, config_digest_of_image, image_store, read_lock

from harness.release_pair import DEFAULT, ReleasePair
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
INFRA_EXIT = 75
"""실행 환경 탓으로 멈출 때의 종료 코드(EX_TEMPFAIL) — Windows 쪽 `golden.wsl`이 `WslError`(infra)로
바꾼다(`golden.WSL_INFRA_EXIT`와 같다). 그 밖의 0 아닌 종료는 제품 스크립트 실패(product)다."""


def infra_exit(message: str) -> SystemExit:
    """`raise infra_exit(...)` — 메시지를 stderr에 쓰고 `INFRA_EXIT`로 끝내는 SystemExit."""
    print(message, file=sys.stderr, flush=True)
    return SystemExit(INFRA_EXIT)


def _run(*cmd: str, cwd: Path = ROOT) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


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


def nightly_prune(names: dict[str, float], current: str, keep: int = NIGHTLY_KEEP) -> list[str]:
    """지울 nightly 이름: `names`(이름 → store 디렉터리 mtime) 가운데 `current`와 그 밖의 최근
    `keep - 1`개를 뺀 나머지(오래된 순)."""
    others = sorted((n for n in names if n != current), key=lambda n: names[n], reverse=True)
    return sorted(others[max(keep - 1, 0) :], key=lambda n: names[n])


def _docker_ok() -> bool:
    try:
        done = subprocess.run(
            ("docker", "info"), capture_output=True, timeout=DOCKER_INFO_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def _save(tag: str, tar: Path) -> dict[str, str]:
    _run("docker", "save", "-o", str(tar), tag)
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
            ("docker", "build", "-q", "-t", tag, "-"), input=dockerfile, text=True, check=True
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
        ("git", "-C", str(src), "rev-parse", "HEAD"), capture_output=True, text=True, check=True
    ).stdout.strip()


def _build_head(src: Path, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", "deploy/build/build_images.sh", *NIGHTLY_BUILD),
        cwd=src,
        env={**os.environ, "PUREIRON_RELEASE": f"{name}-src"},
        capture_output=True,
        text=True,
        check=False,
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
) -> dict[str, object]:
    """HEAD(`src`) 빌드 → 라벨 파생 → `W/releases/<name>.json`·`W/store/<name>/`·시뮬레이터 tar.
    빌드 실패는 `{"ok": false}`(제품 실패), 사본 커밋 불일치·빌드 뒤 Docker 무응답·BASE 기록/store
    없음은 `INFRA_EXIT`(인프라 실패), 빌드가 성공한 뒤의 다른 예외는 exit 1(제품 실패)이다.
    `git_head`·`build_head`·`docker_ok`·`store_root`·`records`는 단위 시험이 바꿔 끼운다."""
    head = git_head(src)
    if head != sha:
        raise infra_exit(f"{src} is at {head}, not {sha}")
    build = build_head(src, name)
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
    relink(store / base, real_store / base)
    base_record = cast(dict[str, object], json.loads(base_path.read_text(encoding="utf-8")))
    images = nightly_images(store, real_store, src, name=name, sha=sha, base_record=base_record)
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
    sim_out.parent.mkdir(parents=True, exist_ok=True)
    _run("docker", "save", "-o", str(sim_out), f"pureiron/simulator:{name}-src")
    nightly = {d.name: d.stat().st_mtime for d in store.glob("n*") if d.is_dir()}
    for old in nightly_prune(nightly, name):
        shutil.rmtree(store / old)
        for svc in NIGHTLY_BUILD:
            for tag in (f"pureiron/{svc}:{old}", f"pureiron/{svc}:{old}-src"):
                subprocess.run(("docker", "image", "rm", tag), capture_output=True, check=False)
    return {"ok": True, "name": name, "record": record, "simulator_tar": str(sim_out)}


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
) -> Path:
    fleet = work / "sites" / f"{site_id}-g{gen}.yaml"
    fleet.parent.mkdir(parents=True, exist_ok=True)
    fleet.write_text(
        f"site: {site_id}\ngeneration: {gen}\nproduct: {product}\n"
        "project: { repo: pureiron-project-acme, version: v1 }\n"
        f'window: {{ once: {{ start: "{start}", end: "{end}" }} }}\n',
        encoding="utf-8",
    )
    out.mkdir(parents=True, exist_ok=True)
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
        str(KEYS / "release.key"),
        "--cert-dir",
        str(KEYS),
        "--root-pub",
        str(KEYS / "test-root.pub"),
        "--fleet-commit",
        "e2e0000",
        "--out",
        str(out),
        "--releases",
        str(work / "releases"),
        "--store",
        str(work / "store"),
    )
    return out / f"pureiron-{site_id}-g{gen}.pib"


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


def absorb(work: Path, draft: Path, site_key: Path, site_id: str) -> dict[str, object]:
    """ⓓ: 아직 absorb하지 않은 repo 사본에 현장 드래프트를 회수하고 게이트를 사전 점검한다."""
    repo, absorbed = work / "m5" / "repo", work / "m5" / "absorbed"
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
    m5 = sub.add_parser("m5-project")
    m5.add_argument("--work", type=Path, required=True)
    absorb_cmd = sub.add_parser("absorb")
    absorb_cmd.add_argument("--work", type=Path, required=True)
    absorb_cmd.add_argument("--draft", type=Path, required=True)
    absorb_cmd.add_argument("--site-key", type=Path, required=True)
    absorb_cmd.add_argument("--site", default="site-a")
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
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.command == "fault-release":
        print(fault_release(args.work, DEFAULT))
    elif args.command == "site":
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
            )
        )
    elif args.command == "m5-project":
        print(m5_project(args.work))
    elif args.command == "absorb":
        print(json.dumps(absorb(args.work, args.draft, args.site_key, args.site)))
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
    else:
        print(install(args.work, args.out, args.release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
