"""현장 → control-api 송신(M4-1): heartbeat(E4)·보고(E6). 서비스 tick이 poll 뒤 advance 전에 부른다.

- **heartbeat**: tick마다 1건 `POST /sites/{site}/heartbeat`(`site-heartbeat` 봉투). seq는 보내기
  전에 공용 카운터에서 예약한다(`outbox.reserve_seq`, fsync). 실패하면 버린다(늦은 heartbeat는
  쓸모없다 — 다음 tick이 새로 보낸다). payload:
  `{site_id, report_seq, current, updater_phase, disk_state, at}`.
  - `current` = 적용된 generation(`compose/g<gen>/manifest.json`)의
    `{product, project_version, gen}` — 시도가 진행 중이어도 적용된 값(N-3). 아직 적용 전이면
    null, manifest를 못 읽으면 버전만 null.
  - `updater_phase` = 경보가 있으면 `failed_manual`, 진행 중 시도가 있으면 그 phase, 없으면 `idle`.
  - `disk_state` = `PUREIRON_ROOT` 사용률 ≥ 0.90 `critical`, ≥ 0.80 `warn`, 그 밖 `ok`.
- **보고**: 대기 보고를 오래된 순으로 tick당 최대 20건 `POST /sites/{site}/reports`(`site-report`
  봉투 = 큐 파일 + `site_id`·`report_seq`). seq는 `outbox.assign`(처음 매긴 값 하나, N-b).
  - 200(applied·history) → `reports/exported/`.
  - 400(형식 위반 — 중앙이 영원히 받지 않음) → `reports/rejected/` + 오류 로그 + 카운터(큐 맨 앞을
    막지 않게, 파일은 보존).
  - 401·403·404·5xx·연결 실패 → 그대로 두고 이 tick의 보고 전송을 멈춘다(다음 tick에 같은 seq로).
- 미등록·`seq_pending`(재등록 뒤 rekey 응답 미반영)이면 둘 다 보내지 않는다(경고 로그).
- 각 단계의 예외는 로그만 남긴다 — heartbeat가 실패해도 보고는 보내고, 둘 다 apply를 막지 않는다.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, dumps, sign_document
from pureiron_bundle.trust import format_timestamp
from pureiron_updater.compose import read_manifest, target_dir
from pureiron_updater.enroll import EnrollError
from pureiron_updater.metrics import (
    EXPORTED_DIR,
    REJECTED_DIR,
    load_counters,
    refresh_outbox_metrics,
    save_counters,
)
from pureiron_updater.outbox import (
    SEQ_KEY,
    SeqPendingError,
    Signer,
    assign,
    move,
    pending,
    reserve_seq,
    settle_journal,
    signer,
)
from pureiron_updater.poll import SendError
from pureiron_updater.settings import Settings
from pureiron_updater.state import UpdaterState

log = logging.getLogger(__name__)

REPORTS_PER_TICK = 20
DISK_WARN_RATIO = 0.80
DISK_CRITICAL_RATIO = 0.90
ENVELOPE_TYPE = "application/json"
IDLE = "idle"
FAILED_MANUAL = "failed_manual"


class Poster(Protocol):
    """`poll.ControlApi`의 송신 표면(테스트는 fake)."""

    def site_path(self, suffix: str) -> str: ...

    def post(self, path: str, data: bytes, content_type: str) -> tuple[int, bytes]: ...


class DiskState(StrEnum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


def disk_state(used_ratio: float) -> DiskState:
    if used_ratio >= DISK_CRITICAL_RATIO:
        return DiskState.CRITICAL
    if used_ratio >= DISK_WARN_RATIO:
        return DiskState.WARN
    return DiskState.OK


def used_ratio(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total if usage.total else 0.0


def updater_phase(state: UpdaterState) -> str:
    if state.alarm is not None:
        return FAILED_MANUAL
    return IDLE if state.attempt is None else state.attempt.phase.value


def current_of(settings: Settings, state: UpdaterState) -> Json:
    gen = state.applied_gen
    if gen is None:
        return None
    try:
        manifest = read_manifest(target_dir(settings.compose_dir, gen))
    except (BundleRejectedError, OSError) as exc:
        log.warning("heartbeat: cannot read manifest of generation %s: %s", gen, exc)
        return {"product": None, "project_version": None, "gen": gen}
    return {
        "product": manifest.product_release.version,
        "project_version": manifest.project.version,
        "gen": gen,
    }


def heartbeat_payload(
    settings: Settings, state: UpdaterState, *, site_id: str, report_seq: int, now: datetime
) -> dict[str, Json]:
    return {
        "site_id": site_id,
        "report_seq": report_seq,
        "current": current_of(settings, state),
        "updater_phase": updater_phase(state),
        "disk_state": disk_state(used_ratio(settings.root)).value,
        "at": format_timestamp(now),
    }


def _envelope(sign: Signer, purpose: SitePurpose, payload: dict[str, Json]) -> bytes:
    return dumps(sign_document(sign.seed, purpose, payload))


def _detail(body: bytes) -> str:
    return body.decode("utf-8", "replace").strip()[:500]


def send_heartbeat(
    settings: Settings, state: UpdaterState, api: Poster, sign: Signer, *, now: datetime
) -> bool:
    """heartbeat 1건. 중앙이 받았으면(200) True. 실패는 버린다(재전송 없음)."""
    seq = reserve_seq(settings.state_dir)
    payload = heartbeat_payload(settings, state, site_id=sign.site_id, report_seq=seq, now=now)
    envelope = _envelope(sign, SitePurpose.HEARTBEAT, payload)
    try:
        status, body = api.post(api.site_path("heartbeat"), envelope, ENVELOPE_TYPE)
    except SendError as exc:
        log.warning("heartbeat %s not sent: %s", seq, exc)
        return False
    if status != 200:
        log.warning("heartbeat %s refused: HTTP %s %s", seq, status, _detail(body))
        return False
    counters = load_counters(settings.state_dir)
    save_counters(settings.state_dir, replace(counters, heartbeat_last_success=now.timestamp()))
    return True


@dataclass(frozen=True, slots=True)
class ReportsSent:
    sent: int = 0
    rejected: int = 0
    stopped: bool = False
    """중앙이 받지 못해(401·403·404·5xx·연결) 이 tick의 전송을 멈췄다."""


def send_reports(
    settings: Settings, api: Poster, sign: Signer, *, limit: int = REPORTS_PER_TICK
) -> ReportsSent:
    """대기 보고를 오래된 순으로 최대 `limit`건 보낸다(응답별 처리는 모듈 docstring)."""
    state_dir = settings.state_dir
    settle_journal(state_dir)
    sent = rejected = 0
    for path in pending(state_dir)[:limit]:
        item = assign(state_dir, path)
        payload: dict[str, Json] = {**item.body, "site_id": sign.site_id, SEQ_KEY: item.report_seq}
        envelope = _envelope(sign, SitePurpose.REPORT, payload)
        try:
            status, body = api.post(api.site_path("reports"), envelope, ENVELOPE_TYPE)
        except SendError as exc:
            log.warning("report %s (seq %s) not sent, kept: %s", path.name, item.report_seq, exc)
            return ReportsSent(sent, rejected, stopped=True)
        if status == 200:
            move(path, EXPORTED_DIR)
            sent += 1
        elif status == 400:
            moved = move(path, REJECTED_DIR)
            counters = load_counters(state_dir)
            save_counters(state_dir, replace(counters, rejected_total=counters.rejected_total + 1))
            log.error(
                "report seq %s rejected by control-api (HTTP 400 %s) — moved to %s",
                item.report_seq,
                _detail(body),
                moved,
            )
            rejected += 1
        else:
            log.warning(
                "report seq %s: HTTP %s %s — kept for the next tick",
                item.report_seq,
                status,
                _detail(body),
            )
            return ReportsSent(sent, rejected, stopped=True)
    return ReportsSent(sent, rejected)


def send_all(settings: Settings, state: UpdaterState, api: Poster, *, now: datetime) -> None:
    """tick의 송신 단계: heartbeat → 보고. 단계마다 예외는 로그만(서로·apply를 막지 않는다)."""
    try:
        sign = signer(settings)
    except SeqPendingError as exc:
        log.warning("not sending heartbeat/reports: %s", exc)
        return
    except (EnrollError, OSError) as exc:
        log.warning("not sending heartbeat/reports (site key): %s", exc)
        return
    try:
        send_heartbeat(settings, state, api, sign, now=now)
    except Exception:
        log.exception("heartbeat failed")
    try:
        send_reports(settings, api, sign)
    except Exception:
        log.exception("sending reports failed")
    try:
        refresh_outbox_metrics(settings.state_dir)
    except OSError as exc:
        log.warning("cannot write outbox metrics: %s", exc)
