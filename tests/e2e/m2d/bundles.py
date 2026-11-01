"""M2d 번들 재료(빌드 호스트 = WSL runner, 서명 키는 `~/pureiron/keys/test`).

    uv run --no-sync python tests/e2e/m2d/bundles.py fault-release --work W
    uv run --no-sync python tests/e2e/m2d/bundles.py site --work W --out O --gen 2 \\
        --product 2026.10.3-fault1 --window-start 2026-10-05T00:00:00Z --window-end ...
    uv run --no-sync python tests/e2e/m2d/bundles.py install --work W --out O --release 2026.10.2
    uv run --no-sync python tests/e2e/m2d/bundles.py m5-project --work W
    uv run --no-sync python tests/e2e/m2d/bundles.py absorb --work W --draft D --site-key E

release 쌍(`BASE`·`NEXT`·`FAULT`)은 `release_pair.py`가 정한다(M5 마감: r3→r4, fault r4f).

- `fault-release`: `FAULT`(r4', `2026.11.0-fault1`) — `NEXT` 이미지 그대로에 collector만 세션을 맺지
  않는 파생 이미지(`sleep`). core는 healthy라 `up --wait`는 통과하고 `/readyz`(collector 세션)가
  실패해 `checking`에서 `E_HEALTH` → 롤백을 일으킨다. 저장소 `deploy/releases`는 건드리지 않고
  `W/releases`(저장소 release 기록 전부의 사본 + `FAULT`)와 `W/store`(`BASE`·`NEXT`는 실제 image
  store를 가리키는 symlink)에 둔다. 기록을 전부 옮기는 것은 `compat_app_min`이 저장소와 같게
  나오도록이다.
- `site`: fleet 현장 파일을 쓰고 `deploy/bundle/build_bundle.py`로 서명 번들을 만든다(release는
  `--releases W/releases`에서 `version == --product`로 찾는다). 프로젝트 트리는 `--project-dir`
  (기본 fixture `acme`).
- `m5-project`(M5 확인 ⓒ·ⓓ): `W/m5/repo` = fixture `acme` 사본에서 `project.yaml absorbs`를 뺀 것 —
  "아직 absorb하지 않은 고객 repo". fixture는 `absorbs: {site-a: 3}`라 그대로 쓰면 VM의 현장
  site_rev(PATCH 2번 = 2)가 3 이하여서 드래프트 게이트가 통과하고(ⓒ가 `E_DRAFT`를 못 본다) absorb는
  리비전 퇴행(2 < 3) 충돌이 된다(ⓓ). 그 밖의 파일은 fixture와 바이트까지 같다.
- `absorb`(ⓓ): `W/m5/repo`를 `W/m5/absorbed`로 새로 복사하고 `pureiron-project absorb`(VM이
  서명한 draft.tar + VM `enrollment.json`) → `pureiron-project gate --draft`를 차례로 실행한다. 각
  종료 코드와 출력을 JSON 한 줄로 내고 판정은 `run.py`가 한다(실패해도 exit 0 — 결과에 증거를
  남기려고).
- `install`: `fetch_debs.sh` → `build_updater.sh` → `build_install_bundle.py`(실빌드, 네트워크
  필요). 설치 번들은 이미지를 싣지 않는다 — `--release`는 manifest `release`·파일 이름에만 쓰인다.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "build"))

from images import config_digest_from_tar, config_digest_of_image, image_store
from release_pair import BASE, FAULT, NEXT, RELEASES

from pureiron_project import canonical
from pureiron_project.project import load_project

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "packages" / "project-tools" / "fixtures" / "acme"

KEYS = Path.home() / "pureiron" / "keys" / "test"
FAULT_NAME, FAULT_VERSION, FAULT_BASE = FAULT, RELEASES[FAULT], NEXT
APP_SERVICES = ("core", "collector", "web")
FAULT_DOCKERFILE = """\
FROM pureiron/collector:{base}
# e2e fault: collector가 core에 gRPC 세션을 맺지 않는다 → core /readyz 503 → Updater E_HEALTH.
ENTRYPOINT ["sleep", "infinity"]
CMD []
"""


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


def fault_release(work: Path) -> Path:
    releases, store = work / "releases", work / "store"
    releases.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    real_store = image_store()
    for name in (BASE, NEXT):
        if not (ROOT / "deploy" / "releases" / f"{name}.json").is_file():
            raise SystemExit(f"deploy/releases/{name}.json missing (release_record.py {name})")
        if not (real_store / name).is_dir():
            raise SystemExit(f"image store {real_store / name} missing")
    # 이전 쌍(r0→r1 등)의 사본·파생 기록이 남지 않게 기록 디렉터리를 저장소와 맞춘다.
    for old in releases.glob("*.json"):
        old.unlink()
    for record_path in sorted((ROOT / "deploy" / "releases").glob("*.json")):
        shutil.copy2(record_path, releases / record_path.name)
    for name in (BASE, NEXT):
        relink(store / name, real_store / name)
    base = json.loads((releases / f"{FAULT_BASE}.json").read_text(encoding="utf-8"))
    base_images = cast(dict[str, dict[str, str]], base["images"])
    fault_store = store / FAULT_NAME
    fault_store.mkdir(exist_ok=True)

    build_dir = work / "fault-collector"
    build_dir.mkdir(exist_ok=True)
    (build_dir / "Dockerfile").write_text(FAULT_DOCKERFILE.format(base=FAULT_BASE), "utf-8")
    # digest 계산용 임시 tar는 docker build context(build_dir) 밖에 둔다.
    scratch = work / "digest-scratch"
    for svc in APP_SERVICES:
        ensure_image(
            f"pureiron/{svc}:{FAULT_BASE}",
            real_store / FAULT_BASE / f"{svc}.tar",
            base_images[svc]["config_digest"],
            digest_of=lambda tag: local_digest(tag, scratch),
            load=lambda tar: _run("docker", "load", "-q", "-i", str(tar)),
        )
    _run("docker", "build", "-q", "-t", f"pureiron/collector:{FAULT_NAME}", str(build_dir))
    images: dict[str, dict[str, str]] = {}
    for svc in APP_SERVICES:
        tag = f"pureiron/{svc}:{FAULT_NAME}"
        if svc != "collector":
            _run("docker", "tag", f"pureiron/{svc}:{FAULT_BASE}", tag)
        tar = fault_store / f"{svc}.tar"
        _run("docker", "save", "-o", str(tar), tag)
        images[svc] = {"tag": tag, "config_digest": config_digest_from_tar(tar)}
        # core·web은 FAULT_BASE 이미지 그대로여야 한다(collector만 파생) — 롤백 판정이
        # 이 digest를 쓴다.
        want = base_images[svc]["config_digest"]
        if svc != "collector" and images[svc]["config_digest"] != want:
            raise SystemExit(f"{tag}: config digest {images[svc]['config_digest']} != {want}")
    relink(fault_store / "site-db.tar", real_store / FAULT_BASE / "site-db.tar")
    images["site-db"] = base_images["site-db"]
    record = {
        "name": FAULT_NAME,
        "release": FAULT_NAME,
        "version": FAULT_VERSION,
        "commit": base["commit"],
        "images": images,
        "schema": base["schema"],
    }
    path = releases / f"{FAULT_NAME}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


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
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.command == "fault-release":
        print(fault_release(args.work))
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
    else:
        print(install(args.work, args.out, args.release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
