"""n-minus-1 게이트(G1-5, ADR-024): 직전 epoch 앱(N-1)의 통합 테스트를 현재 스키마(N)에서 돌린다.

1. 현재 카탈로그의 epoch N을 구한다. N == 1이면 직전 release가 없어 notice 후 통과(bootstrap).
2. `deploy/releases/*.json`에서 `schema.app_expected == N-1`인 기록 중 번호가 가장 큰 것을 고르고,
   그 core-test 이미지를 확보한다(로컬에 없으면 image store의 tar를 load). 실행 전에 이미지
   config digest가 기록과 같은지 확인한다 — 같은 태그가 다른 빌드로 덮어써졌으면 실패.
3. 현재 커밋의 core를 `pureiron/core:ci-<sha>`로 빌드하고, 임시 docker network의 site-db에
   그 이미지로 `migrate up`(N), 압축 chunk·CAGG 데이터를 넣은 뒤 N-1 core-test 이미지를
   `PUREIRON_TEST_EXISTING_DSN` 모드로 실행한다.
4. 테스트와 별개로 N-1 이미지의 `pureiron-core migrate status --json`(N-1 앱 자신의 판정)이
   `cur == N`, `app_expected == N-1`, envelope 안, checksum 정상인지 본다 — 롤백된 N-1 앱이
   N 스키마에서 ready가 되는 근거를 테스트 선택과 무관하게 직접 확인한다.
5. `n_minus_1_waivers.json`에 그 N-1 release로 등록된 테스트만 `--deselect` 한다. N-1 이미지는
   동결이라 고칠 수 없으므로, 앱 동작이 아니라 스키마 모양을 지나치게 좁게 단정한 테스트만
   사유와 함께 올린다(예: 전역 개수 단정). waiver 테스트는 따로 돌려 **여전히 실패하는지**
   확인한다(통과하면 낡은 waiver라 게이트 실패). 본 실행은 통과 ≥ 1, deselect 수 == waiver 수,
   실패 0이어야 한다. 건너뛴 테스트는 `::warning::`으로 남긴다.
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
WAIVERS = Path(__file__).resolve().parent / "n_minus_1_waivers.json"
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
# CALL refresh_continuous_aggregate는 트랜잭션(DO 블록 포함) 안에서 못 돌아 CAGG마다 따로 부른다.
REFRESH_CALLS_SQL = (
    "SELECT format('CALL refresh_continuous_aggregate(%L, NULL, NULL)', "
    "format('%I.%I', view_schema, view_name)) "
    "FROM timescaledb_information.continuous_aggregates ORDER BY view_name"
)
_NUMBERED = re.compile(r"^r(\d+)$")
# 정확한 테스트 함수 하나(pytest `--deselect`는 접두사 비교라 열린 형태를 받지 않는다).
_NODE_ID = re.compile(r"^[\w./-]+\.py::test_\w+(\[[^\]]*\])?$")
_SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|deselected|errors?)\b")


class GateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Waiver:
    test: str
    reason: str


def load_waivers(path: Path, release: str) -> list[Waiver]:
    """`{release: [{test, reason}]}`에서 이 N-1 release의 항목. 사유 없는 항목은 거부한다."""
    if not path.exists():
        return []
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GateError(f"{path.name}: top level must be an object")
    entries: object = cast(dict[str, object], data).get(release, [])
    if not isinstance(entries, list):
        raise GateError(f"{path.name}: {release} must be a list")
    waivers: list[Waiver] = []
    for entry in cast(list[object], entries):
        item = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        test, reason = str(item.get("test", "")).strip(), str(item.get("reason", "")).strip()
        if not _NODE_ID.match(test) or not reason:
            raise GateError(
                f"{path.name}: {release} entry needs an exact pytest node id "
                f"(path.py::test_name) and a reason: {test!r}"
            )
        waivers.append(Waiver(test, reason))
    return waivers


def pytest_counts(output: str) -> dict[str, int]:
    """pytest 마지막 요약 줄(`1 passed, 9 skipped, 1 deselected in 0.7s`)의 개수."""
    lines = [line for line in output.splitlines() if " in " in line and _SUMMARY.search(line)]
    if not lines:
        return {}
    counts: dict[str, int] = {}
    for number, kind in _SUMMARY.findall(lines[-1]):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(number)
    return counts


def check_main_run(returncode: int, counts: dict[str, int], waivers: list[Waiver]) -> str | None:
    """본 실행 판정 — 문제가 있으면 이유, 없으면 None."""
    if returncode != 0 or counts.get("failed", 0) or counts.get("error", 0):
        return f"N-1 tests failed (exit {returncode}, {counts})"
    if counts.get("passed", 0) < 1:
        return f"N-1 run executed no passing test ({counts})"
    if counts.get("deselected", 0) != len(waivers):
        return f"deselected {counts.get('deselected', 0)} tests, expected {len(waivers)} waivers"
    return None


def check_envelope(
    status: dict[str, object], previous_epoch: int, current_epoch: int
) -> str | None:
    """N-1 앱 `migrate status --json` 판정 — N 스키마에서 ready 조건을 만족하는지."""
    cur, expected, maximum = (
        status.get("cur"),
        status.get("app_expected"),
        status.get("app_max_compatible"),
    )
    if cur != current_epoch or expected != previous_epoch:
        return (
            f"N-1 app sees cur={cur} app_expected={expected}, want {current_epoch}/{previous_epoch}"
        )
    if not isinstance(maximum, int) or not previous_epoch <= current_epoch <= maximum:
        return f"schema epoch {current_epoch} is outside N-1 envelope [{expected}, {maximum}]"
    if status.get("checksum_ok") is not True:
        return f"N-1 app reports checksum mismatch: {status.get('mismatched')}"
    return None


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


def _psql(container: str, sql: str) -> str:
    return _docker(
        "exec",
        container,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "pureiron",
        "-d",
        "pureiron",
        "-At",
        "-c",
        sql,
    ).stdout


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


def _run_core_test(
    image: str, network: str, dsn: str, *args: str, entrypoint: str | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "run", "--rm", "--network", network]
    cmd += ["-e", f"PUREIRON_TEST_EXISTING_DSN={dsn}", "-e", f"PUREIRON_DB_DSN={dsn}"]
    if entrypoint is not None:
        cmd += ["--entrypoint", entrypoint]
    return subprocess.run(
        [*cmd, image, *args], capture_output=True, text=True, encoding="utf-8", check=False
    )


def run_gate(
    previous: ReleaseRecord, current_release: str, current_epoch: int, waivers: list[Waiver]
) -> int:
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
        for call in _psql(db, REFRESH_CALLS_SQL).splitlines():
            _psql(db, call)
        return _judge(previous, network, dsn, current_epoch, waivers)
    finally:
        _docker("rm", "-f", db, check=False)
        _docker("network", "rm", network, check=False)


def _judge(
    previous: ReleaseRecord, network: str, dsn: str, current_epoch: int, waivers: list[Waiver]
) -> int:
    problems: list[str] = []
    status_run = _run_core_test(
        previous.core_test, network, dsn, "migrate", "status", "--json", entrypoint="pureiron-core"
    )
    try:
        status = cast(dict[str, object], json.loads(status_run.stdout))
    except ValueError:
        status = {}
    print(f"n-minus-1: {previous.name} migrate status: {status_run.stdout.strip()}")
    problem = check_envelope(status, previous.app_expected, current_epoch)
    if problem:
        problems.append(f"{problem} (stderr: {status_run.stderr.strip()[-500:]})")

    for waiver in waivers:
        waived = _run_core_test(
            previous.core_test,
            network,
            dsn,
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            "-q",
            waiver.test,
            entrypoint="pytest",
        )
        counts = pytest_counts(waived.stdout)
        if counts != {"failed": 1}:
            problems.append(
                f"waiver {waiver.test} must still fail exactly once (stale or not matching): "
                f"exit {waived.returncode}, {counts}"
            )

    deselect = [arg for w in waivers for arg in ("--deselect", w.test)]
    main_run = _run_core_test(previous.core_test, network, dsn, *deselect)
    print(main_run.stdout)
    if main_run.stderr:
        print(main_run.stderr, file=sys.stderr)
    problem = check_main_run(main_run.returncode, pytest_counts(main_run.stdout), waivers)
    if problem:
        problems.append(problem)
    for problem in problems:
        print(f"::error::n-minus-1: {problem}", file=sys.stderr)
    return 1 if problems else 0


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
        waivers = load_waivers(WAIVERS, previous.name)
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
    for waiver in waivers:
        print(f"::warning::n-minus-1 waived {waiver.test} ({previous.name}): {waiver.reason}")
    try:
        return run_gate(previous, current_release, current_epoch, waivers)
    finally:
        # 커밋마다 생기는 ci-<sha> 이미지를 남기지 않는다(단일 runner 디스크, PD-24).
        _docker("image", "rm", "-f", f"pureiron/core:{current_release}", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
