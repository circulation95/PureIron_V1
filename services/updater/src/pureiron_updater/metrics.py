"""시도 결과 기록: ApplyAttempt 보고 대기열(`state/reports/`)과 textfile 메트릭(`state/metrics/`).

- `reports/<started>-g<gen>-<sha256 12자>.json`은 control-api `POST /sites/{site}/reports`
  (plan §6.4) 본문 필드를 담는다. `report_seq`는 처음 나갈 때(온라인 전송이든 `report export`든)
  `outbox.assign`이 파일에 써 넣고, site key 서명은 보낼 때 붙인다(`send.py`·`outbox.py`).
- 보고의 문자열은 저장 가능한 UTF-8로 정리해 쓴다(`storable_text`): 명령 출력에서 온 짝 없는
  서로게이트는 JCS 서명(`CanonicalizationError`)을, NUL은 중앙 저장(400)을 막아 큐 맨 앞을
  영원히 막는다(M4-0 리뷰 r3).
- `metrics/updater.prom`(plan §4.2): `updater_last_result`, `updater_applied_generation`,
  `updater_failed_manual`(alarm 유무 — `failed_manual` 또는 `E_DEFINITIONS_LEFT`),
  `updater_phase_duration_seconds{phase}`(마지막 시도) + 송신 줄(`outbox_lines`, M4-1):
  `updater_reports_pending`, `updater_reports_rejected_total`,
  `updater_heartbeat_last_success_timestamp_seconds`. 송신은 시도 종료와 따로 일어나므로 송신 줄만
  바꿔 쓴다(`refresh_outbox_metrics`). 송신 카운터는 `state/outbox.json`에 둔다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from pureiron_bundle.manifest import Json
from pureiron_bundle.verify import AttemptResult
from pureiron_updater.state import Attempt, Phase, UpdaterState, atomic_write

REPORTS_DIR = "reports"
EXPORTED_DIR = "exported"
"""`reports/` 아래 — 중앙이 받았거나 서명 묶음에 들어간 보고."""
REJECTED_DIR = "rejected"
"""`reports/` 아래 — 중앙이 형식 위반(400)으로 거부한 보고(보존)."""
METRICS_DIR = "metrics"
PROM_FILE = "updater.prom"
OUTBOX_FILE = "outbox.json"
"""`state/` 아래 송신 카운터(`rejected_total`, `heartbeat_last_success`)."""
_OUTBOX_METRICS = (
    "updater_reports_pending",
    "updater_reports_rejected_total",
    "updater_heartbeat_last_success_timestamp_seconds",
)


_UNESCAPABLE = re.compile("[\ud800-\udc7f]")
"""`surrogateescape`가 바이트로 되돌릴 수 없는 서로게이트(\udc80-\udcff만 되돌린다)."""


def storable_text(text: str) -> str:
    """UTF-8로 인코딩되고 NUL이 없는 문자열로. `surrogateescape`로 읽힌 바이트는 원래 바이트로
    되돌려 UTF-8로 깨진 부분만 U+FFFD로, 그 밖의 짝 없는 서로게이트는 `?`로, NUL은 U+FFFD로."""
    raw = _UNESCAPABLE.sub("?", text).encode("utf-8", "surrogateescape")
    return raw.decode("utf-8", "replace").replace("\x00", "\ufffd")


def storable(value: Json) -> Json:
    """`storable_text`를 모든 문자열 값·키에 적용한다(중첩 포함)."""
    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, dict):
        return {storable_text(k): storable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [storable(item) for item in value]
    return value


def phase_durations(attempt: Attempt, ended_at: datetime) -> dict[str, float]:
    entries = sorted(attempt.timeline.items(), key=lambda item: item[1])
    durations: dict[str, float] = {}
    for i, (phase, entered) in enumerate(entries):
        left = entries[i + 1][1] if i + 1 < len(entries) else ended_at
        durations[phase] = round(durations.get(phase, 0.0) + (left - entered).total_seconds(), 3)
    return durations


def result_of(attempt: Attempt) -> AttemptResult:
    return {
        Phase.SUCCEEDED: AttemptResult.SUCCEEDED,
        Phase.REJECTED: AttemptResult.REJECTED,
    }.get(attempt.phase, AttemptResult.FAILED)


def write_report(state_dir: Path, attempt: Attempt, ended_at: datetime) -> Path:
    report: dict[str, Json] = {
        "generation": attempt.generation,
        "from": attempt.from_generation,
        "to": attempt.generation,
        "phase": (attempt.failed_phase or attempt.phase).value,
        "result": result_of(attempt).value,
        "failed_manual": attempt.phase is Phase.FAILED_MANUAL,
        "code": attempt.code,
        "logs": attempt.detail or "",
        "verify_s": attempt.verify_s,
        "load_s": attempt.load_s,
        "bundle_sha256": attempt.bundle_sha256,
        "started_at": attempt.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "phase_durations_s": cast(Json, phase_durations(attempt, ended_at)),
        "timeline": {phase: ts.isoformat() for phase, ts in attempt.timeline.items()},
    }
    directory = state_dir / REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # 파일 이름은 시도 시작 시각 + 번들 sha256 앞부분 — `_finish`가 crash 뒤 다시 돌아도 같은 파일을
    # 덮어쓰고, 같은 순간 거부된 서로 다른 번들(generation 미상)끼리는 겹치지 않는다.
    stamp = attempt.started_at.strftime("%Y%m%dT%H%M%S%fZ")
    gen = "unknown" if attempt.generation is None else str(attempt.generation)
    bundle = attempt.bundle_sha256.removeprefix("sha256:")[:12]
    path = directory / f"{stamp}-g{gen}-{bundle}.json"
    done = [directory / sub / path.name for sub in (EXPORTED_DIR, REJECTED_DIR)]
    if any(p.exists() for p in done):
        # crash 뒤 `_finish` 재실행 전에 이미 나간 보고(`report export`가 끼어든 경우) — 다시 큐에
        # 넣으면 같은 보고가 다른 seq로 또 나간다(N-b).
        return path
    try:
        seq = json.loads(path.read_text(encoding="utf-8")).get("report_seq")
    except (FileNotFoundError, ValueError, AttributeError):
        seq = None
    if type(seq) is int:
        report["report_seq"] = seq  # 이미 매긴 seq는 다시 쓸 때도 유지한다
    write_report_file(path, report)
    return path


def write_report_file(path: Path, report: Mapping[str, Json]) -> None:
    """보고 파일 하나를 (다시) 쓴다 — 문자열 정리 + 원자적 교체."""
    text = json.dumps(storable(dict(report)), indent=2, sort_keys=True, ensure_ascii=False)
    atomic_write(path, (text + "\n").encode("utf-8"))


@dataclass(frozen=True, slots=True)
class OutboxCounters:
    rejected_total: int = 0
    heartbeat_last_success: float | None = None
    """마지막으로 중앙이 받은 heartbeat의 시각(Unix 초)."""


def load_counters(state_dir: Path) -> OutboxCounters:
    try:
        data: object = json.loads((state_dir / OUTBOX_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return OutboxCounters()  # 카운터는 관측용 — 깨졌으면 0부터 다시 센다
    doc = cast(dict[str, object], data) if isinstance(data, dict) else {}
    rejected, last = doc.get("rejected_total"), doc.get("heartbeat_last_success")
    return OutboxCounters(
        rejected_total=rejected if type(rejected) is int else 0,
        heartbeat_last_success=float(last) if isinstance(last, int | float) else None,
    )


def save_counters(state_dir: Path, counters: OutboxCounters) -> None:
    body = {
        "rejected_total": counters.rejected_total,
        "heartbeat_last_success": counters.heartbeat_last_success,
    }
    atomic_write(state_dir / OUTBOX_FILE, (json.dumps(body, sort_keys=True) + "\n").encode())


def outbox_lines(state_dir: Path) -> list[str]:
    queue = state_dir / REPORTS_DIR
    pending = sum(1 for _ in queue.glob("*.json")) if queue.is_dir() else 0
    counters = load_counters(state_dir)
    last = counters.heartbeat_last_success
    return [
        "# TYPE updater_reports_pending gauge",
        f"updater_reports_pending {pending}",
        "# TYPE updater_reports_rejected_total counter",
        f"updater_reports_rejected_total {counters.rejected_total}",
        "# TYPE updater_heartbeat_last_success_timestamp_seconds gauge",
        f"updater_heartbeat_last_success_timestamp_seconds {0 if last is None else last}",
    ]


def _is_outbox_line(line: str) -> bool:
    name = line.removeprefix("# TYPE ").split(" ", 1)[0].split("{", 1)[0]
    return name in _OUTBOX_METRICS


def refresh_outbox_metrics(state_dir: Path) -> None:
    """`updater.prom`의 송신 줄만 바꿔 쓴다(시도 줄은 그대로, 파일이 없으면 송신 줄만)."""
    path = state_dir / METRICS_DIR / PROM_FILE
    try:
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not _is_outbox_line(line)
        ]
    except FileNotFoundError:
        kept = []
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, ("\n".join([*kept, *outbox_lines(state_dir)]) + "\n").encode())


def write_prom(
    state_dir: Path, state: UpdaterState, last: Attempt | None, ended_at: datetime
) -> None:
    lines = ["# TYPE updater_last_result gauge"]
    for result in AttemptResult:
        value = 1 if state.last_result is result else 0
        lines.append(f'updater_last_result{{result="{result.value}"}} {value}')
    lines += [
        "# TYPE updater_applied_generation gauge",
        f"updater_applied_generation {-1 if state.applied_gen is None else state.applied_gen}",
        "# TYPE updater_failed_manual gauge",
        f"updater_failed_manual {0 if state.alarm is None else 1}",
        "# TYPE updater_phase_duration_seconds gauge",
    ]
    if last is not None:
        for phase, seconds in sorted(phase_durations(last, ended_at).items()):
            lines.append(f'updater_phase_duration_seconds{{phase="{phase}"}} {seconds}')
    lines += outbox_lines(state_dir)
    directory = state_dir / METRICS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / PROM_FILE, ("\n".join(lines) + "\n").encode())
