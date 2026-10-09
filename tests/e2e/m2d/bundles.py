"""M2d 번들 재료(빌드 호스트 = WSL runner, 서명 키는 `~/pureiron/keys/test`).

    uv run --no-sync python tests/e2e/m2d/bundles.py fault-release --work W
    uv run --no-sync python tests/e2e/m2d/bundles.py site --work W --out O --gen 2 \\
        --product 2026.10.2-fault1 --window-start 2026-10-05T00:00:00Z --window-end ...
    uv run --no-sync python tests/e2e/m2d/bundles.py install --work W --out O --release 2026.10.1

release 쌍(`BASE`·`NEXT`·`FAULT`)은 `release_pair.py`가 정한다(S3: r1→r2, fault r2f).

- `fault-release`: `FAULT`(r2', `2026.10.2-fault1`) — `NEXT` 이미지 그대로에 collector만 세션을 맺지
  않는 파생 이미지(`sleep`). core는 healthy라 `up --wait`는 통과하고 `/readyz`(collector 세션)가
  실패해 `checking`에서 `E_HEALTH` → 롤백을 일으킨다. 저장소 `deploy/releases`는 건드리지 않고
  `W/releases`(저장소 release 기록 전부의 사본 + `FAULT`)와 `W/store`(`BASE`·`NEXT`는 실제 image
  store를 가리키는 symlink)에 둔다. 기록을 전부 옮기는 것은 `compat_app_min`이 저장소와 같게
  나오도록이다.
- `site`: fleet 현장 파일을 쓰고 `deploy/bundle/build_bundle.py`로 서명 번들을 만든다(release는
  `--releases W/releases`에서 `version == --product`로 찾는다).
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "build"))

from images import config_digest_from_tar, image_store
from release_pair import BASE, FAULT, NEXT, RELEASES

ROOT = Path(__file__).resolve().parents[3]

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


def ensure_image(tag: str, tar: Path) -> None:
    """`tag`가 로컬 docker에 없으면 image store의 tar(`docker save`로 태그 포함)에서 적재한다."""
    inspect = subprocess.run(("docker", "image", "inspect", tag), capture_output=True, check=False)
    if inspect.returncode != 0:
        _run("docker", "load", "-q", "-i", str(tar))
    _run("docker", "image", "inspect", "--format", "{{.Id}}", tag)


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
        link = store / name
        if not link.is_symlink():
            link.symlink_to(real_store / name, target_is_directory=True)
    base = json.loads((releases / f"{FAULT_BASE}.json").read_text(encoding="utf-8"))
    fault_store = store / FAULT_NAME
    fault_store.mkdir(exist_ok=True)

    build_dir = work / "fault-collector"
    build_dir.mkdir(exist_ok=True)
    (build_dir / "Dockerfile").write_text(FAULT_DOCKERFILE.format(base=FAULT_BASE), "utf-8")
    for svc in APP_SERVICES:
        ensure_image(f"pureiron/{svc}:{FAULT_BASE}", real_store / FAULT_BASE / f"{svc}.tar")
    _run("docker", "build", "-q", "-t", f"pureiron/collector:{FAULT_NAME}", str(build_dir))
    images: dict[str, dict[str, str]] = {}
    for svc in APP_SERVICES:
        tag = f"pureiron/{svc}:{FAULT_NAME}"
        if svc != "collector":
            _run("docker", "tag", f"pureiron/{svc}:{FAULT_BASE}", tag)
        tar = fault_store / f"{svc}.tar"
        _run("docker", "save", "-o", str(tar), tag)
        images[svc] = {"tag": tag, "config_digest": config_digest_from_tar(tar)}
    site_db = fault_store / "site-db.tar"
    if not site_db.exists():
        site_db.symlink_to(real_store / FAULT_BASE / "site-db.tar")
    images["site-db"] = base["images"]["site-db"]
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
    work: Path, out: Path, *, gen: int, product: str, start: str, end: str, site_id: str
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
        str(ROOT / "packages" / "project-tools" / "fixtures" / "acme"),
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
            )
        )
    else:
        print(install(args.work, args.out, args.release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
