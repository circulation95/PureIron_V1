"""현장 번들(`pureiron-<site>-g<gen>.pib`) 빌더(ADR-023, plan §6.2).

    uv run python deploy/bundle/build_bundle.py \\
        --fleet-file sites/site-a.yaml --project-dir ../pureiron-project-acme \\
        --release-key ~/pureiron/keys/test/release.key --cert-dir ~/pureiron/keys/test \\
        --fleet-commit abc123 --out out/bundles

입력:
- fleet 현장 파일(fleet-site 계약 검증): site, generation, product(달력 버전), project 버전, window.
- release 기록 `deploy/releases/*.json` 중 `version == product`인 것: 이미지 config digest·태그,
  schema epoch. `compat_app_min` = `app_max_compatible ≥ 이 release epoch`인 가장 오래된 release.
- image store(`images.image_store()`)의 `<release>/<svc>.tar`(release_record.py가 보관):
  core·collector·web·site-db를 결정적 gzip(mtime 0, 파일명 없음)으로 `images/<svc>.tar.gz`에
  넣고, 압축본에서 다시 읽은 config digest가 기록과 같은지 확인한다.
- 프로젝트 디렉터리(project-tools로 검증): 정규형 파일을 결정적 tar `project/project.tar`로.
- release key(raw 32B seed)와 root가 발급한 `release-cert.json`·`.sig`.

출력: `.pib`와 같은 디렉터리의 `bundle-metrics.json`(`size_bytes`, `image_sizes`, `build_s`).
크기 예산(PD-6): 1.5 GiB 초과 → 실패, 1.2 GiB 초과 → `::warning::`.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import sys
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))

from images import image_store

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejected
from pureiron_bundle.images import ImageFormatError, config_digest_from_tar
from pureiron_bundle.manifest import (
    SITE_IMAGES,
    Json,
    SchemaName,
    release_version_pattern,
    validate,
)
from pureiron_bundle.pib import CERT_FILE, CERT_SIG, CHUNK, tar_info
from pureiron_bundle.sign import write_signed_bundle
from pureiron_bundle.trust import format_timestamp, parse_timestamp
from pureiron_project.canonical import CanonicalError, dump, load
from pureiron_project.project import PROJECT_FILE, Project, load_project

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "deploy" / "releases"
UPDATER_PYPROJECT = ROOT / "services" / "updater" / "pyproject.toml"
GIB = 1024**3
SIZE_LIMIT = int(1.5 * GIB)
SIZE_WARN = int(1.2 * GIB)
METRICS_FILE = "bundle-metrics.json"


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    name: str
    version: str
    images: dict[str, dict[str, str]]
    epoch: int
    app_expected: int
    app_max_compatible: int


def version_key(version: str) -> tuple[int, int, int, int, str]:
    """달력 버전 정렬 키. 접미사(`-rc.1` 등)가 있으면 같은 번호의 정식보다 앞선다."""
    core, _, suffix = version.partition("-")
    year, month, number = (int(part) for part in core.split("."))
    return year, month, number, 0 if suffix else 1, suffix


def load_releases(directory: Path) -> list[ReleaseRecord]:
    """`version`이 있는 release 기록(버전 순)."""
    records: list[ReleaseRecord] = []
    for path in sorted(directory.glob("*.json")):
        data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        version = data.get("version")
        if not isinstance(version, str):
            continue
        if not release_version_pattern().match(version):
            raise BuildError(f"{path.name}: version {version!r} is not YYYY.M.N")
        schema = cast(dict[str, int], data["schema"])
        records.append(
            ReleaseRecord(
                name=str(data["name"]),
                version=version,
                images=cast(dict[str, dict[str, str]], data["images"]),
                epoch=schema["epoch"],
                app_expected=schema["app_expected"],
                app_max_compatible=schema["app_max_compatible"],
            )
        )
    return sorted(records, key=lambda r: version_key(r.version))


def find_release(records: Sequence[ReleaseRecord], version: str) -> ReleaseRecord:
    matches = [r for r in records if r.version == version]
    if len(matches) != 1:
        raise BuildError(
            f"expected exactly one release record with version {version}, got {len(matches)}"
        )
    return matches[0]


def compat_app_min(records: Sequence[ReleaseRecord], epoch: int) -> str:
    """이 release 스키마(epoch)에서 동작하는 가장 오래된 앱 release 버전."""
    compatible = [r for r in records if r.app_max_compatible >= epoch]
    if not compatible:
        raise BuildError(f"no release record is compatible with schema epoch {epoch}")
    return min(compatible, key=lambda r: version_key(r.version)).version


def default_min_updater() -> str:
    data = tomllib.loads(UPDATER_PYPROJECT.read_text(encoding="utf-8"))
    return str(cast(dict[str, object], data["project"])["version"])


def load_fleet_site(path: Path) -> dict[str, Json]:
    try:
        document = load(path.read_text(encoding="utf-8"), path.name)
    except CanonicalError as exc:
        raise BuildError(str(exc)) from exc
    problems = validate(SchemaName.FLEET_SITE, document)
    if problems:
        raise BuildError(f"{path.name}: " + "; ".join(problems))
    return cast(dict[str, Json], document)


def compress_image(source: Path, target: Path) -> None:
    """결정적 gzip(mtime 0, 파일명 없음), 스트리밍."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        source.open("rb") as src,
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as dst,
    ):
        shutil.copyfileobj(src, dst, CHUNK)


def write_project_tar(project: Project, target: Path) -> None:
    """정규형 프로젝트 파일들의 결정적 tar(경로 UTF-8 바이트 순)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(project.files, key=lambda f: f.relpath.encode("utf-8"))
    with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as tar:
        for f in ordered:
            data = dump(f.data).encode("utf-8")
            tar.addfile(tar_info(f.relpath, len(data)), io.BytesIO(data))


def size_gate(size: int) -> int:
    """PD-6 크기 예산. 0 = 통과(경고 포함), 1 = 초과."""
    if size > SIZE_LIMIT:
        print(f"::error::bundle is {size} bytes (> 1.5 GiB budget)")
        return 1
    if size > SIZE_WARN:
        print(f"::warning::bundle is {size} bytes (> 1.2 GiB warning threshold)")
    return 0


def _project_document(project: Project) -> dict[str, Json]:
    for f in project.files:
        if f.relpath == PROJECT_FILE and isinstance(f.data, dict):
            return f.data
    raise BuildError(f"{PROJECT_FILE} missing from project")


def stage_images(
    record: ReleaseRecord, store: Path, staging: Path
) -> tuple[dict[str, Path], dict[str, Json], dict[str, int]]:
    """store의 release 이미지 tar → `images/<svc>.tar.gz`(payload, manifest images, 크기)."""
    payload: dict[str, Path] = {}
    images: dict[str, Json] = {}
    sizes: dict[str, int] = {}
    for svc in SITE_IMAGES:
        source = store / record.name / f"{svc}.tar"
        if not source.is_file():
            raise BuildError(f"{source} missing (release_record.py {record.name} keeps it)")
        name = f"images/{svc}.tar.gz"
        target = staging / name
        compress_image(source, target)
        recorded = record.images[svc]
        try:
            actual = config_digest_from_tar(target)
        except ImageFormatError as exc:
            raise BuildError(str(exc)) from exc
        if actual != recorded["config_digest"]:
            raise BuildError(
                f"{svc}: config digest {actual} != release record {recorded['config_digest']}"
            )
        entry: dict[str, Json] = {"config_digest": actual, "tag": recorded["tag"]}
        if "upstream" in recorded:
            entry["upstream"] = recorded["upstream"]
        images[svc] = entry
        sizes[svc] = target.stat().st_size
        payload[name] = target
    return payload, images, sizes


def build(args: argparse.Namespace) -> int:
    started = time.monotonic()
    fleet = load_fleet_site(args.fleet_file)
    records = load_releases(args.releases)
    record = find_release(records, cast(str, fleet["product"]))
    project = load_project(args.project_dir)
    if not project.ok:
        raise BuildError("project invalid: " + "; ".join(project.problems))
    project_doc = _project_document(project)
    fleet_project = cast(dict[str, Json], fleet["project"])

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    pib = out / f"pureiron-{fleet['site']}-g{fleet['generation']}.pib"
    store: Path = args.store or image_store()
    with tempfile.TemporaryDirectory(dir=out, prefix=".staging-") as tmp:
        staging = Path(tmp)
        payload, images, image_sizes = stage_images(record, store, staging)
        project_tar = staging / "project" / "project.tar"
        write_project_tar(project, project_tar)
        payload["project/project.tar"] = project_tar

        project_ref: dict[str, Json] = {
            "project_id": project_doc["project_id"],
            "version": fleet_project["version"],
            "content_hash": project.content_hash(),
        }
        if "absorbs" in project_doc:
            project_ref["absorbs"] = project_doc["absorbs"]
        created_at = parse_timestamp(args.created_at) if args.created_at else datetime.now(UTC)
        document: dict[str, Json] = {
            "format": 1,
            "kind": "site",
            "site_id": fleet["site"],
            "generation": fleet["generation"],
            "created_at": format_timestamp(created_at),
            "product_release": {
                "version": record.version,
                "min_updater": args.min_updater or default_min_updater(),
                "images": images,
                "schema": {
                    "target": record.epoch,
                    "app_expected": record.app_expected,
                    "app_max_compatible": record.app_max_compatible,
                    "compat_app_min": compat_app_min(records, record.epoch),
                },
            },
            "project": project_ref,
            "window": fleet["window"],
            "trust_update": None,
            "fleet_commit": args.fleet_commit,
        }
        cert_dir: Path = args.cert_dir
        write_signed_bundle(
            pib,
            document,
            payload,
            release_seed=keys.read_private_key(args.release_key),
            cert_json=(cert_dir / CERT_FILE).read_bytes(),
            cert_sig=(cert_dir / CERT_SIG).read_bytes(),
        )
    size = pib.stat().st_size
    metrics = {
        "size_bytes": size,
        "image_sizes": image_sizes,
        "build_s": round(time.monotonic() - started, 3),
    }
    (out / METRICS_FILE).write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(pib)
    return size_gate(size)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_bundle")
    p.add_argument("--fleet-file", type=Path, required=True, help="fleet sites/<site>.yaml")
    p.add_argument("--project-dir", type=Path, required=True, help="고객 project repo(태그)")
    p.add_argument("--release-key", type=Path, required=True, help="release key raw 32B seed")
    p.add_argument("--cert-dir", type=Path, required=True, help="release-cert.json/.sig 위치")
    p.add_argument("--fleet-commit", required=True, help="fleet repo 커밋(hex)")
    p.add_argument("--out", type=Path, required=True, help="출력 디렉터리")
    p.add_argument("--releases", type=Path, default=RELEASES, help="release 기록 디렉터리")
    p.add_argument("--store", type=Path, default=None, help="이미지 tar 보관소(기본 image_store())")
    p.add_argument("--min-updater", default=None, help="기본: services/updater 버전")
    p.add_argument("--created-at", default=None, help="RFC 3339(기본: 지금, 테스트용)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return build(args)
    except (BuildError, BundleRejected, ValueError, OSError) as exc:
        print(f"::error::build_bundle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
