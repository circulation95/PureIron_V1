"""release 기록(`deploy/releases/<name>.json`): 이미지 config digest + 스키마 epoch envelope.

    python deploy/build/release_record.py r0 [--release 0.1.0-dev]

로컬에 로드된 `pureiron/<svc>:<release>` 이미지에서 config digest를 읽는다. n-minus-1 게이트는
이 기록으로 직전 epoch의 core-test 이미지를 찾는다. 현장 manifest(M2)의 product_release 원천이다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from images import config_digest_of_image, read_lock

from pureiron_core.platform.migrate import load_catalog

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "deploy" / "releases"
RECORD_SERVICES = ("core", "collector", "web", "core-test")

def build_record(name: str, release: str) -> dict[str, object]:
    catalog = load_catalog(ROOT / "services" / "core" / "migrations")
    lock = read_lock(RELEASES / "site-db.lock")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        images: dict[str, dict[str, str]] = {
            svc: {
                "tag": f"pureiron/{svc}:{release}",
                "config_digest": config_digest_of_image(f"pureiron/{svc}:{release}", Path(tmp)),
            }
            for svc in RECORD_SERVICES
        }
        site_db_tag = f"pureiron/site-db:{lock['version']}"
        images["site-db"] = {
            "tag": site_db_tag,
            "config_digest": config_digest_of_image(site_db_tag, Path(tmp)),
            "upstream": f"{lock['upstream']}@{lock['digest']}",
        }
    return {
        "name": name,
        "release": release,
        "commit": commit,
        "images": images,
        "schema": {
            "epoch": catalog.app_expected,
            "app_expected": catalog.app_expected,
            "app_max_compatible": catalog.app_max_compatible,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--release", default="0.1.0-dev")
    args = parser.parse_args()
    record = build_record(args.name, args.release)
    out = RELEASES / f"{args.name}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
