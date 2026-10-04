"""n-minus-1 게이트(G1-5, ADR-024): 직전 epoch 앱(N-1)의 통합 테스트를 현재 스키마(N)에서 돌린다.

1. 현재 카탈로그의 epoch N을 구한다. N == 1이면 직전 release가 없어 notice 후 통과(bootstrap).
2. `deploy/releases/*.json`에서 `schema.app_expected == N-1`인 기록 중 번호가 가장 큰 것을 고르고,
   그 core-test 이미지를 확보한다(로컬에 없으면 image store의 tar를 load). 실행 전에 이미지
   config digest가 기록과 같은지 확인한다 — 같은 태그가 다른 빌드로 덮어써졌으면 실패.
3. 현재 커밋의 core를 `pureiron/core:ci-<sha>`로 빌드하고, 임시 docker network의 site-db에
   그 이미지로 `migrate up`(N), 압축 chunk·CAGG 데이터를 넣은 뒤 N-1 core-test 이미지를
   `PUREIRON_TEST_EXISTING_DSN` 모드로 실행한다.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from images import bash_executable, config_digest_of_image, image_store, read_lock

from pureiron_core.platform.migrate import load_catalog

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "deploy" / "releases"
SEED_SQL = """
INSERT INTO devices (id, protocol) VALUES ('n1-dev', 'modbus_tcp') ON CONFLICT DO NOTHING;
INSERT INTO tags (path, device_id) VALUES ('n1/tag', 'n1-dev') ON CONFLICT DO NOTHING;
INSERT INTO tag_history (time, tag_id, value_num, quality)
SELECT ts, (SELECT id FROM tags WHERE path = 'n1/tag'), extract(epoch FROM ts), 1
  FROM generate_series(now() - interval '5 days', now() - interval '3 days', interval '1 minute') ts
ON CONFLICT DO NOTHING;
SELECT compress_chunk(c, if_not_compressed => true)
  FROM show_chunks('tag_history', older_than => interval '2 days') c;
"""
REFRESH_SQL = "CALL refresh_continuous_aggregate('tag_history_1m', NULL, NULL)"
_NUMBERED = re.compile(r"^r(\d+)$")


class GateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    name: str
    release: str
    app_expected: int
    core_test: str
    core_test_digest: str

    @property
    def order(self) -> tuple[int, str]:
        """`r10`이 `r2`보다 뒤에 오도록 숫자 순."""
        match = _NUMBERED.match(self.name)
        return (int(match[1]) if match else -1, self.name)


def load_records(directory: Path) -> list[ReleaseRecord]:
    records: list[ReleaseRecord] = []
    for path in directory.glob("*.json"):
        data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        schema = cast(dict[str, int], data.get("schema", {}))
        images = cast(dict[str, dict[str, str]], data.get("images", {}))
        if "app_expected" not in schema or "core-test" not in images:
            continue
        records.append(
            ReleaseRecord(
                name=str(data["name"]),
                release=str(data["release"]),
                app_expected=schema["app_expected"],
                core_test=images["core-test"]["tag"],
                core_test_digest=images["core-test"]["config_digest"],
            )
        )
    return sorted(records, key=lambda r: r.order)


def pick_previous(records: list[ReleaseRecord], current_epoch: int) -> ReleaseRecord | None:
    candidates = [r for r in records if r.app_expected == current_epoch - 1]
    return max(candidates, key=lambda r: r.order) if candidates else None


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", check=check
    )


def _psql(container: str, sql: str) -> None:
    _docker(
        "exec",
        container,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "pureiron",
        "-d",
        "pureiron",
        "-c",
        sql,
    )


def ensure_image(record: ReleaseRecord, store: Path) -> None:
    """N-1 core-test 이미지를 확보하고 기록된 config digest와 대조한다."""
    if _docker("image", "inspect", record.core_test, check=False).returncode != 0:
        tar = store / record.name / "core-test.tar"
        if not tar.exists():
            raise GateError(f"{record.core_test} not loaded and {tar} missing")
        _docker("load", "-i", str(tar))
    with tempfile.TemporaryDirectory() as tmp:
        actual = config_digest_of_image(record.core_test, Path(tmp))
    if actual != record.core_test_digest:
        raise GateError(
            f"{record.core_test} config digest {actual} != recorded {record.core_test_digest}"
        )


def run_gate(previous: ReleaseRecord, current_release: str) -> int:
    lock = read_lock(RELEASES / "site-db.lock")
    current_core = f"pureiron/core:{current_release}"
    suffix = uuid.uuid4().hex[:8]
    network, db = f"pureiron-n1-{suffix}", f"pureiron-n1-db-{suffix}"
    password = secrets.token_urlsafe(16)
    dsn = f"postgresql://pureiron:{password}@{db}:5432/pureiron"
    _docker("network", "create", network)
    try:
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            db,
            "--network",
            network,
            "-e",
            "POSTGRES_USER=pureiron",
            "-e",
            "POSTGRES_DB=pureiron",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            f"pureiron/site-db:{lock['version']}",
        )
        for _ in range(60):
            ready = _docker(
                "exec", db, "pg_isready", "-U", "pureiron", "-d", "pureiron", check=False
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        _docker(
            "run",
            "--rm",
            "--network",
            network,
            "-e",
            f"PUREIRON_DB_DSN={dsn}",
            current_core,
            "migrate",
            "up",
        )
        _psql(db, SEED_SQL)
        _psql(db, REFRESH_SQL)
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "-e",
                f"PUREIRON_TEST_EXISTING_DSN={dsn}",
                previous.core_test,
            ],
            check=False,
        )
        return result.returncode
    finally:
        _docker("rm", "-f", db, check=False)
        _docker("network", "rm", network, check=False)


def main() -> int:
    current_epoch = load_catalog(ROOT / "services" / "core" / "migrations").app_expected
    if current_epoch == 1:
        print("::notice::schema epoch 1 is the baseline — no N-1 release to test yet (bootstrap)")
        return 0
    previous = pick_previous(load_records(RELEASES), current_epoch)
    if previous is None:
        print(
            f"::error::no release record with schema.app_expected == {current_epoch - 1}",
            file=sys.stderr,
        )
        return 1
    try:
        ensure_image(previous, image_store())
    except GateError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    # 현재 커밋의 core를 고유 태그로 빌드한다 — runner를 공유하는 다른 브랜치 이미지와 섞이지 않게.
    current_release = (
        "ci-"
        + subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    subprocess.run(
        [bash_executable(), "deploy/build/build_images.sh", "core"],
        cwd=ROOT,
        env={**os.environ, "PUREIRON_RELEASE": current_release},
        check=True,
    )
    print(
        f"n-minus-1: {previous.core_test} (epoch {previous.app_expected}) "
        f"against schema epoch {current_epoch} (core {current_release})"
    )
    try:
        return run_gate(previous, current_release)
    finally:
        # 커밋마다 생기는 ci-<sha> 이미지를 남기지 않는다(단일 runner 디스크, PD-24).
        _docker("image", "rm", "-f", f"pureiron/core:{current_release}", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
