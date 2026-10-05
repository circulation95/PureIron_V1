"""시도 결과 기록: ApplyAttempt 보고 대기열(`state/reports/`)과 textfile 메트릭(`state/metrics/`).

- `reports/<started>-g<gen>-<sha256 12자>.json`은 control-api `POST /sites/{site}/reports`
  (plan §6.4) 본문 필드를 담는다. `report_seq`·site key 서명은 `report export`(폐쇄망 USB
  반출, `enroll.py`)가 묶을 때 붙이고, 온라인 전송은 control-api(M4) 몫이다.
- `metrics/updater.prom`(plan §4.2): `updater_last_result`, `updater_applied_generation`,
  `updater_failed_manual`, `updater_phase_duration_seconds{phase}`(마지막 시도).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pureiron_bundle.verify import AttemptResult
from pureiron_updater.state import Attempt, Phase, UpdaterState, atomic_write

REPORTS_DIR = "reports"
METRICS_DIR = "metrics"
PROM_FILE = "updater.prom"


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
    report = {
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
        "phase_durations_s": phase_durations(attempt, ended_at),
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
    atomic_write(path, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    return path


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
    directory = state_dir / METRICS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / PROM_FILE, ("\n".join(lines) + "\n").encode())
