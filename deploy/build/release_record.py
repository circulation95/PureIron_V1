"""release 기록(`deploy/releases/<name>.json`): 이미지 config digest + 스키마 epoch envelope.

    PUREIRON_RELEASE=r0 bash deploy/build/build_images.sh
    python deploy/build/release_record.py r0 [--store DIR]

`pureiron/<svc>:<name>`(release 이름이 곧 고유 태그)에서 config digest를 읽고, 이미지를
`<store>/<name>/<svc>.tar`로 보관한다(기본 store: `images.image_store()`). n-minus-1 게이트는
이 기록의 core-test 이미지를 store에서 다시 load하고 digest를 대조한다. 현장 manifest(M2)의
product_release 원천이기도 하다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from images import config_digest_from_tar, config_digest_of_image, image_store, read_lock

from pureiron_core.platform.migrate import load_catalog

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "deploy" / "releases"
RECORD_SERVICES = ("core", "collector", "web", "core-test")


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


def build_record(name: str, store: Path) -> dict[str, object]:
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("release_record: working tree has uncommitted changes")
    catalog = load_catalog(ROOT / "services" / "core" / "migrations")
    lock = read_lock(RELEASES / "site-db.lock")
    target = store / name
    target.mkdir(parents=True, exist_ok=True)
    images: dict[str, dict[str, str]] = {}
    head = _git("rev-parse", "HEAD")
    for svc in RECORD_SERVICES:
        tag = f"pureiron/{svc}:{name}"
        built_from = _image_revision(tag)
        if built_from != head:
            raise SystemExit(
                f"release_record: {tag} was built from {built_from or '?'}, not {head}"
            )
        tar = target / f"{svc}.tar"
        subprocess.run(["docker", "save", "-o", str(tar), tag], check=True)
        images[svc] = {"tag": tag, "config_digest": config_digest_from_tar(tar)}
    site_db_tag = f"pureiron/site-db:{lock['version']}"
    with tempfile.TemporaryDirectory() as tmp:
        images["site-db"] = {
            "tag": site_db_tag,
            "config_digest": config_digest_of_image(site_db_tag, Path(tmp)),
            "upstream": f"{lock['upstream']}@{lock['digest']}",
        }
    return {
        "name": name,
        "release": name,
        "commit": head,
        "images": images,
        "schema": {
            "epoch": catalog.app_expected,
            "app_expected": catalog.app_expected,
            "app_max_compatible": catalog.app_max_compatible,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="release 이름 = 이미지 태그(예: r0)")
    parser.add_argument("--store", type=Path, default=None, help="이미지 tar 보관 위치")
    args = parser.parse_args()
    record = build_record(args.name, args.store or image_store())
    out = RELEASES / f"{args.name}.json"
    out.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
