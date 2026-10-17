"""2,000태그 1s 부하 측정(M3a-3 계획 §4 부하, D10: 기록만, 합격선 없음).

`bash scripts/g1.sh perf-ingest`로만 실행한다(`integration-compose`는 `not perf`로 제외).
시뮬레이터 레지스터 수는 compose 기동 전에 `PUREIRON_SIM_REGISTERS=2000`으로 넘긴다.
결과: `artifacts/perf/<PUREIRON_PERF_RELEASE, 기본 r3>/ingest.json`.
r3(M3b-2)부터 알람 정의 `PERF_ALARMS`개(사인파 중간값 setpoint)와 모든 태그를 구독한 WS 연결
1개를 붙인 상태로 잰다(bus 핸들러가 ingest를 늦추는지 — 계획 R1). WS 수신 통계도 남긴다.

시간은 pytest 프로세스의 monotonic으로 잰다. Docker Desktop(WSL2)에서는 pytest·컨테이너가 같은 VM
시계를 쓰고, 그 monotonic이 호스트보다 느릴 수 있다(M3a-3 W1 실측 -7%) — 그러면 초당 값은 "VM 초"
기준이라 호스트 초 기준보다 그만큼 크다. 결과에 VM·호스트 경과를 함께 남긴다.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import asyncpg
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .conftest import (
    CORE_PORT,
    REPO,
    Stack,
    above_alarm,
    api,
    compose,
    core_cli,
    ensure_user,
    holding_tag,
    login_token,
    modbus_device,
    wait_http,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.perf, pytest.mark.compose]

CORE = f"http://127.0.0.1:{CORE_PORT}/api/v1"
TAGS = int(os.environ.get("PUREIRON_PERF_TAGS", "2000"))
MEASURE_S = float(os.environ.get("PUREIRON_PERF_MEASURE_S", "60"))
RELEASE = os.environ.get("PUREIRON_PERF_RELEASE", "r3")
PERF_ALARMS = 50
STATS_EVERY_S = 5.0
SERVICES = ("core", "collector", "site-db")
STATS_FORMAT = "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}"


def _readyz() -> dict[str, object]:
    try:
        with urllib.request.urlopen(f"{CORE}/readyz", timeout=3) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    data: dict[str, object] = json.loads(body)
    return data


def _section(data: dict[str, object], name: str) -> dict[str, object]:
    section = data.get(name)
    assert isinstance(section, dict), (name, data)
    return cast("dict[str, object]", section)


def _int(data: dict[str, object], *path: str) -> int:
    *names, key = path
    for name in names:
        data = _section(data, name)
    value = data.get(key)
    assert isinstance(value, int), (path, value)
    return value


def _float(section: dict[str, object], key: str) -> float | None:
    value = section.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))], 3),
        "max": round(ordered[-1], 3),
    }


def _mib(text: str) -> float | None:
    """`docker stats` MemUsage 앞부분(예: `85.2MiB`) → MiB."""
    factors = {"GiB": 1024.0, "MiB": 1.0, "KiB": 1 / 1024, "B": 1 / 1024 / 1024}
    for unit, factor in factors.items():
        if text.endswith(unit):
            try:
                return float(text[: -len(unit)]) * factor
            except ValueError:
                return None
    return None


def _docker_stats(containers: dict[str, str]) -> dict[str, tuple[float, float]]:
    """서비스 → (CPU %, 메모리 MiB). `docker stats --no-stream` 한 번."""
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", STATS_FORMAT, *containers.values()],
        capture_output=True,
        text=True,
        check=False,
    )
    by_id = {cid[:12]: name for name, cid in containers.items()}
    out: dict[str, tuple[float, float]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0][:12] not in by_id:
            continue
        mem = _mib(parts[2].split("/")[0].strip())
        try:
            cpu = float(parts[1].rstrip("%"))
        except ValueError:
            continue
        if mem is not None:
            out[by_id[parts[0][:12]]] = (cpu, mem)
    return out


def _host_seconds() -> float | None:
    """Windows 호스트 시계(초) — WSL에서 돌 때만(VM 시계 오차 기록용). 없으면 None."""
    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout.strip()
        return int(out) / 1000.0
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


async def _window_rows(dsn: str, password: str, start: float, end: float) -> tuple[int, int]:
    conn = await asyncpg.connect(dsn, password=password)
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n, count(DISTINCT tag_id) AS tags FROM tag_history"
            " WHERE time >= to_timestamp($1) AND time < to_timestamp($2)",
            start,
            end,
        )
    finally:
        await conn.close()
    assert row is not None
    return int(row["n"]), int(row["tags"])


class WsStats:
    """WS 수신 통계(측정 스레드가 쓰고 테스트가 읽는다)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.tag_updates = 0
        self.values = 0
        self.alarm_events = 0
        self.close_code: int | None = None

    def reset(self) -> None:
        with self.lock:
            self.tag_updates = self.values = self.alarm_events = 0

    def snapshot(self, elapsed_s: float) -> dict[str, object]:
        with self.lock:
            return {
                "tag_update_messages_per_s": round(self.tag_updates / elapsed_s, 1),
                "tag_values_per_s": round(self.values / elapsed_s, 1),
                "alarm_events": self.alarm_events,
                "closed_code": self.close_code,
            }


def _ws_reader(token: str, stats: WsStats) -> None:
    """모든 태그를 구독하고 받은 메시지를 센다(ping엔 pong)."""
    _, body = api("GET", "/tags", token)
    tag_ids = [t["tag_id"] for t in cast("list[dict[str, object]]", body["tags"])]
    with connect(
        f"ws://127.0.0.1:{CORE_PORT}/api/v1/ws",
        additional_headers={"Cookie": f"pureiron_session={token}"},
        max_size=None,
    ) as ws:
        ws.send(json.dumps({"type": "subscribe", "tag_ids": tag_ids}))
        while not stats.stop.is_set():
            try:
                message = cast("dict[str, object]", json.loads(ws.recv(timeout=1)))
            except TimeoutError:
                continue
            except ConnectionClosed:
                stats.close_code = ws.close_code
                return
            with stats.lock:
                if message["type"] == "tag_update":
                    stats.tag_updates += 1
                    stats.values += len(cast("list[object]", message["values"]))
                elif message["type"] == "alarm_event":
                    stats.alarm_events += 1
            if message["type"] == "ping":
                ws.send(json.dumps({"type": "pong"}))


def test_ingest_2000_tags_1s(stack: Stack, tmp_path: Path) -> None:
    wait_http(f"{CORE}/readyz", timeout_s=120)
    root = write_project(
        tmp_path / "perf-project",
        {"sim": modbus_device("sim", "simulator", 5020)},
        {"sim": [holding_tag(f"sim/r{i:04d}", "sim", i) for i in range(TAGS)]},
        alarms=[
            above_alarm(f"perf-{i:02d}", f"sim/r{i * (TAGS // PERF_ALARMS):04d}", 1000.0)
            for i in range(PERF_ALARMS)
        ],
    )
    applied = core_cli(stack.cli_env("pureiron"), "apply-definitions", str(root), "--json")
    assert applied.returncode == 0, applied.stdout + applied.stderr

    deadline = time.monotonic() + 120
    acquisition: dict[str, object] = {}
    while time.monotonic() < deadline:
        acquisition = _section(_readyz(), "acquisition")
        if acquisition.get("tags_total") == TAGS and acquisition.get("tags_good") == TAGS:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"{TAGS} tags did not become GOOD (PUREIRON_SIM_REGISTERS?): {acquisition}")
    ensure_user(stack, "it-perf", "viewer")
    ws_stats = WsStats()
    ws_thread = threading.Thread(target=_ws_reader, args=(login_token("it-perf"), ws_stats))
    ws_thread.start()
    time.sleep(5)  # 첫 plan 직후 과도 구간을 빼고 잰다
    ws_stats.reset()

    containers = {
        name: compose(stack.compose_env, "ps", "-q", name).stdout.strip() for name in SERVICES
    }
    dsn = stack.dsn("pureiron")
    lags: list[float] = []
    buffers: list[float] = []
    good: list[float] = []
    cpu: dict[str, list[float]] = {name: [] for name in containers}
    mem: dict[str, list[float]] = {name: [] for name in containers}

    first = _readyz()
    host0 = _host_seconds()
    wall0, mono0 = time.time(), time.monotonic()
    next_stats = mono0
    while (now := time.monotonic()) - mono0 < MEASURE_S:
        ready = _readyz()
        historian = _section(ready, "historian")
        if (lag := _float(historian, "write_lag_s")) is not None:
            lags.append(lag)
        buffers.append(_float(historian, "buffer_rows") or 0.0)
        good.append(_float(_section(ready, "acquisition"), "good_ratio") or 0.0)
        if now >= next_stats:
            for name, (c, m) in _docker_stats(containers).items():
                cpu[name].append(c)
                mem[name].append(m)
            next_stats = now + STATS_EVERY_S
        time.sleep(max(0.0, 1.0 - (time.monotonic() - now)))
    last = _readyz()
    elapsed = time.monotonic() - mono0
    ws_snapshot = ws_stats.snapshot(elapsed)
    ws_stats.stop.set()
    ws_thread.join(timeout=10)
    wall1 = time.time()
    host1 = _host_seconds()
    time.sleep(3)  # 마지막 flush(1s 주기)가 끝난 뒤 DB를 센다

    def rate(*path: str) -> float:
        return round((_int(last, *path) - _int(first, *path)) / elapsed, 1)

    window_rows, window_tags = asyncio.run(_window_rows(dsn, stack.password, wall0, wall1))
    historian_last = _section(last, "historian")
    result: dict[str, object] = {
        "release": RELEASE,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tags": TAGS,
        "poll_ms": 1000,
        "clock": {
            "measure_s_vm_monotonic": round(elapsed, 2),
            "measure_s_vm_realtime": round(wall1 - wall0, 2),
            "measure_s_host": round(host1 - host0, 2) if host0 and host1 else None,
            "note": "초당 값은 VM monotonic 초 기준"
            " (VM monotonic이 호스트보다 느리면 그만큼 크게 나온다)",
        },
        "ingest": {
            "batches_per_s": rate("acquisition", "ingest", "batches"),
            "heartbeats_per_s": rate("acquisition", "ingest", "heartbeats"),
            "samples_per_s": rate("acquisition", "ingest", "live"),
            "stale_total": _int(last, "acquisition", "ingest", "stale")
            - _int(first, "acquisition", "ingest", "stale"),
            "future_total": _int(last, "acquisition", "ingest", "future")
            - _int(first, "acquisition", "ingest", "future"),
            "good_ratio_min": min(good) if good else None,
        },
        "historian": {
            "rows_written_per_s": rate("historian", "written_total"),
            "write_lag_s": _summary(lags),
            "buffer_rows_max": max(buffers) if buffers else None,
            "dropped_total": historian_last.get("dropped_total"),
            "rejected_total": historian_last.get("rejected_total"),
            "db_ok": historian_last.get("db_ok"),
        },
        "tag_history": {
            "window_rows": window_rows,
            "window_tags": window_tags,
            "rows_per_minute": round(window_rows * 60 / (wall1 - wall0)),
        },
        "alarms": {"definitions": PERF_ALARMS, **_section(last, "alarms")},
        "ws": ws_snapshot,
        "containers": {
            name: {
                "cpu_pct_mean": round(statistics.fmean(cpu[name]), 1) if cpu[name] else None,
                "cpu_pct_max": round(max(cpu[name]), 1) if cpu[name] else None,
                "rss_mib_max": round(max(mem[name]), 1) if mem[name] else None,
                "samples": len(cpu[name]),
            }
            for name in containers
        },
    }
    out = REPO / "artifacts" / "perf" / RELEASE / "ingest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))  # 기록용(합격선 없음)
    assert historian_last.get("db_ok") is True
