"""release 기록(`deploy/releases/<name>.json`): 이미지 config digest + 스키마 epoch envelope.

    PUREIRON_RELEASE=r0 bash deploy/build/build_images.sh
    python deploy/build/release_record.py r0 --version 2026.10.0 [--store DIR]

    PUREIRON_RELEASE=c0 bash deploy/build/build_images.sh control-api control-web
    python deploy/build/release_record.py c0 --central [--store DIR]

`pureiron/<svc>:<name>`(release 이름이 곧 고유 태그)에서 config digest를 읽고, 이미지를
`<store>/<name>/<svc>.tar`로 보관한다(기본 store: `images.image_store()`; site-db 포함).
n-minus-1 게이트는 이 기록의 core-test 이미지를 store에서 다시 load하고 digest를 대조한다.
현장 manifest의 product_release 원천이기도 하다: `version`(달력 버전 `YYYY.M.N`, fleet
`product:`와 manifest `product_release.version`이 쓰는 값)으로 찾는다. 이미지 태그는
release 이름(`pureiron/<svc>:r<N>`)을 그대로 쓴다.

`--central`(M4-3 G5·G10): 중앙 이미지(control-api·control-web)와 control-api 스키마 epoch를
`deploy/releases/central/<name>.json`에 따로 남긴다. 현장 기록을 읽는 곳(번들 빌더·check-gen·
n-minus-1)은 `deploy/releases/*.json`만 보므로 중앙 이미지는 현장 번들·manifest에 들어가지 않는다.
control-api `migrate lint`는 이 디렉터리를 release 기준으로 본다(`pureiron_control.cli`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from images import config_digest_from_tar, image_store, read_lock

from pureiron_bundle.manifest import release_version_pattern
from pureiron_core.platform.migrate import load_catalog

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "deploy" / "releases"
CENTRAL_RELEASES = RELEASES / "central"
RECORD_SERVICES = ("core", "collector", "web", "core-test")
CENTRAL_SERVICES = ("control-api", "control-web")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _image_revision(tag: str) -> str:
    """build_images.sh가 붙인 `org.opencontainers.image.revision` 라벨(빌드 커밋)."""
    label = '{{index .Config.Labels "org.opencontainers.image.revision"}}'
    return subprocess.run(
        ["docker", "image", "inspect", "-f", label, tag],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _require_clean_tree() -> str:
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("release_record: working tree has uncommitted changes")
    return _git("rev-parse", "HEAD")


def _save_images(
    services: tuple[str, ...], name: str, target: Path, head: str
) -> dict[str, dict[str, str]]:
    """`pureiron/<svc>:<name>`을 `<target>/<svc>.tar`로 보관하고 config digest를 읽는다."""
    target.mkdir(parents=True, exist_ok=True)
    images: dict[str, dict[str, str]] = {}
    for svc in services:
        tag = f"pureiron/{svc}:{name}"
        built_from = _image_revision(tag)
        if built_from != head:
            raise SystemExit(
                f"release_record: {tag} was built from {built_from or '?'}, not {head}"
            )
        tar = target / f"{svc}.tar"
        subprocess.run(["docker", "save", "-o", str(tar), tag], check=True)
        images[svc] = {"tag": tag, "config_digest": config_digest_from_tar(tar)}
    return images


def _schema(migrations: Path) -> dict[str, int]:
    catalog = load_catalog(migrations)
    return {
        "epoch": catalog.app_expected,
        "app_expected": catalog.app_expected,
        "app_max_compatible": catalog.app_max_compatible,
    }


def build_record(name: str, version: str, store: Path) -> dict[str, object]:
    head = _require_clean_tree()
    schema = _schema(ROOT / "services" / "core" / "migrations")
    lock = read_lock(RELEASES / "site-db.lock")
    target = store / name
    images = _save_images(RECORD_SERVICES, name, target, head)
    site_db_tag = f"pureiron/site-db:{lock['version']}"
    site_db_tar = target / "site-db.tar"
    subprocess.run(["docker", "save", "-o", str(site_db_tar), site_db_tag], check=True)
    images["site-db"] = {
        "tag": site_db_tag,
        "config_digest": config_digest_from_tar(site_db_tar),
        "upstream": f"{lock['upstream']}@{lock['digest']}",
    }
    return {
        "name": name,
        "release": name,
        "version": version,
        "commit": head,
        "images": images,
        "schema": schema,
    }


def build_central_record(name: str, store: Path) -> dict[str, object]:
    """중앙 release 기록. `version`(현장 제품 달력 버전)은 없다 — 번들 빌더가 고르지 않게."""
    head = _require_clean_tree()
    return {
        "name": name,
        "release": name,
        "kind": "central",
        "commit": head,
        "images": _save_images(CENTRAL_SERVICES, name, store / "central" / name, head),
        "schema": _schema(ROOT / "services" / "control-api" / "migrations"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="release 이름 = 이미지 태그(예: r0, 중앙은 c0)")
    parser.add_argument("--version", help="제품 달력 버전 YYYY.M.N(현장 기록 필수, 예: 2026.10.0)")
    parser.add_argument(
        "--central", action="store_true", help="중앙 이미지 기록(deploy/releases/central/)"
    )
    parser.add_argument("--store", type=Path, default=None, help="이미지 tar 보관 위치")
    args = parser.parse_args()
    store: Path = args.store or image_store()
    if args.central:
        if args.version is not None:
            parser.error("--central records have no product --version")
        record = build_central_record(args.name, store)
        out = CENTRAL_RELEASES / f"{args.name}.json"
    else:
        if args.version is None or not release_version_pattern().fullmatch(args.version):
            parser.error(
                f"--version must match YYYY.M.N (contract release pattern): {args.version!r}"
            )
        record = build_record(args.name, args.version, store)
        out = RELEASES / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
