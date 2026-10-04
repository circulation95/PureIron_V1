"""repro-build 게이트(G1-7): 같은 커밋을 캐시 없이 두 번 빌드해 config digest가 같은지 본다.

site-db는 업스트림 retag라 제외한다. 결과는 artifacts/repro/report.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from images import SERVICES, bash_executable, config_digest_from_tar

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "artifacts" / "images"
OUT = ROOT / "artifacts" / "repro"


def build_round(label: str) -> dict[str, str]:
    env = {**os.environ, "NO_CACHE": "1", "PUREIRON_RELEASE": f"repro-{label}"}
    subprocess.run(
        [bash_executable(), "deploy/build/build_images.sh", *SERVICES],
        cwd=ROOT,
        env=env,
        check=True,
    )
    keep = OUT / label
    keep.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for svc in SERVICES:
        shutil.copy2(IMAGES / f"{svc}.tar", keep / f"{svc}.tar")
        digests[svc] = config_digest_from_tar(keep / f"{svc}.tar")
    return digests


def compare(first: dict[str, str], second: dict[str, str]) -> list[str]:
    return [svc for svc in SERVICES if first.get(svc) != second.get(svc)]


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    first, second = build_round("a"), build_round("b")
    mismatched = compare(first, second)
    report = {"first": first, "second": second, "mismatched": mismatched}
    (OUT / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    for svc in SERVICES:
        mark = "MISMATCH" if svc in mismatched else "same"
        print(f"{svc:10s} {first[svc]}  {second[svc]}  {mark}")
    if mismatched:
        print(
            f"::error::non-reproducible images: {', '.join(mismatched)} (artifacts/repro/)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
