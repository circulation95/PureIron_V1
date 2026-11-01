"""VM 쪽 상태 수집(M2d). 현장 호스트에서 root로 실행하고 JSON 한 줄을 출력한다.

    python3 probe.py

- `updater`: `state/updater.json` 요약(시도 phase·applied/attempted gen·결과·epoch·경보)
- `service`: `pureiron-updater` 활성 여부·재시작 횟수
- `containers`: compose 프로젝트 `pureiron` 서비스별 이미지 `.Id`(= config digest, PD-19),
  `StartedAt`
- `readyz`: web 경유 `/api/v1/readyz` 상태 코드와 본문, `project`: 편집 잠금(`GET /api/v1/project`).
  둘 다 무인증 경로라 r3(epoch 4)의 사용자 세션 인증 뒤에도 그대로 읽는다
- `db`: schema epoch, CAGG 목록, 압축 chunk 수, `tag_history` 행 수, 실시간 집계 CAGG 목록,
  현재 스키마의 테이블·인덱스·열(`테이블.열`) 목록 — 업그레이드가 더한 산출물을 직접 확인한다.
  프로젝트 정의(M5): `project_files` 행 수(epoch 5), retire되지 않은 장비 id·태그 path·알람 id 목록,
  태그·알람 정의(JSON 문자열, path·id → 정규형 정의) — 트리 적용·현장 PATCH 값을 DB에서 읽는다.
  epoch에 없는 테이블의 항목은 그 항목만 `error`다
- `metrics`: Updater textfile 메트릭(`state/metrics/updater.prom`) 이름(라벨 포함) → 값 —
  `updater_failed_manual`(경보 유무)을 본다
- `docker`: storage driver·image store(classic이면 driver-type 없음)
- `nft`: e2e 감시 테이블(`inet e2e_audit`) 카운터 — 외부로 나가려던 패킷 수

표준 라이브러리만 쓴다(현장 호스트의 /usr/bin/python3.12). 개별 항목 실패는 `error` 문자열로 남긴다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

STATE = Path("/var/lib/pureiron/state/updater.json")
METRICS = Path("/var/lib/pureiron/state/metrics/updater.prom")
WEB = "http://127.0.0.1"
DB_SQL = {
    "epoch": "SELECT value FROM schema_meta WHERE key = 'epoch'",
    "caggs": "SELECT string_agg(view_name, ',' ORDER BY view_name) "
    "FROM timescaledb_information.continuous_aggregates",
    "compressed_chunks": "SELECT count(*) FROM timescaledb_information.chunks "
    "WHERE hypertable_name = 'tag_history' AND is_compressed",
    "tag_history_rows": "SELECT count(*) FROM tag_history",
    # epoch별 산출물은 이름 목록(쉼표 구분, 없으면 "")으로 내고 하네스가 집합으로 판정한다.
    # 실시간 집계 CAGG(0009)
    "realtime_caggs": "SELECT coalesce(string_agg(view_name, ',' ORDER BY view_name), '') "
    "FROM timescaledb_information.continuous_aggregates WHERE NOT materialized_only",
    # 테이블·인덱스(0010~0012 users·sessions·audit_log·alarms·alarm_journal 등)
    "tables": "SELECT coalesce(string_agg(table_name, ',' ORDER BY table_name), '') "
    "FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
    "indexes": "SELECT coalesce(string_agg(indexname, ',' ORDER BY indexname), '') "
    "FROM pg_indexes WHERE schemaname = current_schema()",
    # 열 `테이블.열`(0008 definition·definitions_rev, 0009 historian)
    "columns": "SELECT coalesce(string_agg(table_name || '.' || column_name, ',' "
    "ORDER BY table_name, column_name), '') FROM information_schema.columns "
    "WHERE table_schema = current_schema()",
    # 프로젝트 트리·정의(M5): 트리 적용(0013)·현장 PATCH 값 확인
    "project_files": "SELECT count(*) FROM project_files",
    "device_ids": "SELECT coalesce(string_agg(id, ',' ORDER BY id), '') "
    "FROM devices WHERE retired_at IS NULL",
    "tag_paths": "SELECT coalesce(string_agg(path, ',' ORDER BY path), '') "
    "FROM tags WHERE retired_at IS NULL",
    "alarm_ids": "SELECT coalesce(string_agg(alarm_id, ',' ORDER BY alarm_id), '') "
    "FROM alarms WHERE retired_at IS NULL",
    "tag_defs": "SELECT coalesce(json_object_agg(path, definition ORDER BY path), '{}') "
    "FROM tags WHERE retired_at IS NULL AND definition IS NOT NULL",
    "alarm_defs": "SELECT coalesce(json_object_agg(alarm_id, definition ORDER BY alarm_id), '{}') "
    "FROM alarms WHERE retired_at IS NULL",
}


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60).stdout


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # 수집기는 한 항목이 실패해도 나머지를 낸다
        return {"error": f"{type(exc).__name__}: {exc}"}


def updater() -> dict[str, Any]:
    data = cast(dict[str, Any], json.loads(STATE.read_text(encoding="utf-8")))
    attempt = cast(dict[str, Any], data.get("attempt") or {})
    return {
        "applied_gen": data.get("applied_gen"),
        "last_attempted_gen": data.get("last_attempted_gen"),
        "last_result": data.get("last_result"),
        "schema_epoch": data.get("schema_epoch"),
        "alarm": data.get("alarm"),
        "attempt": {
            "generation": attempt.get("generation"),
            "phase": attempt.get("phase"),
            "code": attempt.get("code"),
        }
        if attempt
        else None,
    }


def service() -> dict[str, Any]:
    props = _run("systemctl", "show", "pureiron-updater", "-p", "ActiveState", "-p", "NRestarts")
    return dict(line.split("=", 1) for line in props.splitlines() if "=" in line)


def containers() -> dict[str, Any]:
    ids = _run(
        "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=pureiron"
    ).split()
    if not ids:
        return {}
    out: dict[str, Any] = {}
    for item in json.loads(_run("docker", "inspect", *ids)):
        service_name = item["Config"]["Labels"].get("com.docker.compose.service", item["Name"])
        out[service_name] = {
            "image_id": item["Image"],
            "image_tag": item["Config"]["Image"],
            "started_at": item["State"]["StartedAt"],
            "running": item["State"]["Running"],
            "restart_count": item.get("RestartCount", 0),
        }
    return out


def http_json(path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{WEB}{path}", timeout=5) as response:
            return {"status": response.status, "body": json.loads(response.read() or b"null")}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed: Any = json.loads(body)
        except ValueError:
            parsed = body.decode(errors="replace")[:500]
        return {"status": exc.code, "body": parsed}


def db() -> dict[str, Any]:
    container = _run(
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=pureiron",
        "--filter",
        "label=com.docker.compose.service=site-db",
    ).strip()
    if not container:
        return {"error": "site-db not running"}
    out: dict[str, Any] = {}
    for key, sql in DB_SQL.items():
        out[key] = _guard(
            lambda sql=sql: _run(
                "docker",
                "exec",
                container,
                "psql",
                "-At",
                "-U",
                "pureiron",
                "-d",
                "pureiron",
                "-c",
                sql,
            ).strip()
        )
    return out


def parse_prom(text: str) -> dict[str, float]:
    """textfile 메트릭 `이름{라벨} 값` 줄(주석·빈 줄 제외) → 이름(라벨 포함) → 값."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name] = float(value)
    return out


def metrics() -> dict[str, float]:
    return parse_prom(METRICS.read_text(encoding="utf-8"))


def docker_info() -> dict[str, Any]:
    info = json.loads(_run("docker", "info", "--format", "{{json .}}"))
    return {
        "server_version": info.get("ServerVersion"),
        "driver": info.get("Driver"),
        "driver_status": info.get("DriverStatus"),
    }


def nft() -> dict[str, Any]:
    data = json.loads(_run("nft", "-j", "list", "table", "inet", "e2e_audit"))
    counters: dict[str, int] = {}
    for entry in data.get("nftables", []):
        rule = entry.get("rule")
        if not rule:
            continue
        name = rule.get("comment") or f"{rule['chain']}#{rule.get('handle')}"
        for expr in rule.get("expr", []):
            if "counter" in expr:
                counters[name] = int(expr["counter"]["packets"])
    return counters


def main() -> int:
    result = {
        "updater": _guard(updater),
        "service": _guard(service),
        "containers": _guard(containers),
        "readyz": _guard(lambda: http_json("/api/v1/readyz")),
        "project": _guard(lambda: http_json("/api/v1/project")),
        "db": _guard(db),
        "docker": _guard(docker_info),
        "nft": _guard(nft),
        "metrics": _guard(metrics),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
