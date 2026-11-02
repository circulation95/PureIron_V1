"""보고 대기열(`state/reports/`)과 `report_seq` 카운터 — 온라인 전송·`report export` 공용(M4-1).

불변식(N-b): **한 보고는 처음 매긴 seq 하나로만 나간다.** 보고가 처음 나갈 때(온라인이든 USB든)
`assign`이 카운터를 예약하고(`site.json` 원자적 교체 = fsync) 그 값을 큐 파일 자체에
`report_seq`로 써 넣는다. 이후 어느 경로로 다시 나가든 파일의 값을 쓴다. 예약 뒤 파일을 쓰기 전에
죽으면 seq 하나가 빈다 — 중앙은 종류별 수위만 보므로 빈 seq는 무해하다.

카운터(`site.json next_report_seq`)는 heartbeat(`send.py`)와 보고가 함께 쓴다(중앙 C7: 공용
카운터, 종류별 수위). `seq_pending`(상태 분실 재등록 뒤 벤더 rekey 응답 미반영)이면 예약을 거부한다.
`reserve_seq`는 예약 전에 r4 저널을 먼저 정리한다 — 어느 경로(heartbeat·보고·USB)의 예약도 저널이
가진 seq를 다시 내주지 않는다(리뷰 M2).

디렉터리:

- `reports/*.json` — 대기(이름 = 시도 시작 시각 순).
- `reports/exported/` — 중앙이 받았거나(온라인 200) 서명 묶음에 들어간(USB) 보고.
- `reports/rejected/` — 중앙이 영원히 받지 않을 보고(형식 위반 400·크기 초과 413)와 JSON으로 읽을
  수 없는 큐 파일. 큐 맨 앞을 막지 않게 빼 두고 오류 로그·메트릭으로 알린다(파일은 보존, `reject`).
  등록 키 불일치(409 — rekey 대기)는 여기로 빼지 않는다(큐에 남겨 다시 보낸다).

r4 호환: r4 `report export`가 끊기며 남긴 저널(`reports/.export.journal{first_seq, names}`)은
어떤 송신보다 먼저 `settle_journal`이 그 seq를 큐 파일에 써 넣고 지운다 — r4가 그 저널로 다시 냈을
묶음과 같은 보고·같은 seq다(이미 `exported/`로 옮겨진 파일은 그 묶음이 이미 쓰였다는 뜻이라 둔다).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, dumps, sign_document
from pureiron_bundle.trust import format_timestamp
from pureiron_updater.enroll import (
    IDENTITY_FILE,
    EnrollError,
    SiteIdentity,
    load_identity,
    save_identity,
)
from pureiron_updater.metrics import (
    EXPORTED_DIR,
    REJECTED_DIR,
    REPORTS_DIR,
    load_counters,
    save_counters,
    storable,
    write_report_file,
)
from pureiron_updater.settings import Settings
from pureiron_updater.state import atomic_write, fsync_dir

log = logging.getLogger(__name__)

JOURNAL_FILE = ".export.journal"
"""r4 `report export` 저널(읽기만 — 새 코드는 쓰지 않는다)."""
SEQ_KEY = "report_seq"


class SeqPendingError(EnrollError):
    """재등록 뒤 다음 seq를 아직 모른다 — 벤더 rekey 응답을 `enroll --next-seq`로 반영해야 한다."""


class UnreadableReportError(EnrollError):
    """큐 파일 내용이 JSON 객체가 아니다(손 편집·잘린 파일) — 영원히 보낼 수 없어 `reject`로 뺀다
    (리뷰 L1)."""


class ReportIOError(EnrollError):
    """큐 파일을 지금 읽을 수 없다(권한·일시 I/O 오류) — 고치면 보낼 수 있으므로 `rejected/`로
    옮기지 않고 큐에 둔다(R2-L1). 송신·USB export는 그 파일만 이번에 건너뛴다."""


@dataclass(frozen=True, slots=True)
class Signer:
    """등록된 현장의 id·지문·서명 키(site.key가 site.json 지문과 맞을 때만)."""

    site_id: str
    fingerprint: str
    seed: bytes


@dataclass(frozen=True, slots=True)
class QueuedReport:
    path: Path
    report_seq: int
    body: dict[str, Json]
    """`report_seq` 포함 — 묶음·온라인 봉투에 그대로 싣는다."""


def queue_dir(state_dir: Path) -> Path:
    return state_dir / REPORTS_DIR


def _require_identity(state_dir: Path) -> SiteIdentity:
    identity = load_identity(state_dir)
    if identity is None:
        raise EnrollError("not enrolled (run: pureiron-site enroll --site <id>)")
    if identity.seq_pending:
        raise SeqPendingError(
            "report_seq is unknown after re-enrollment — apply the vendor's rekey answer first: "
            f"pureiron-site enroll --site {identity.site_id} --next-seq <next_seq>"
        )
    return identity


def signer(settings: Settings) -> Signer:
    """보낼 수 있는 현장인지 확인하고 서명 재료를 돌려준다(미등록·`seq_pending`·키 불일치 거부)."""
    identity = _require_identity(settings.state_dir)
    seed = keys.read_private_key(settings.site_key_file)
    if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
        raise EnrollError(f"{settings.site_key_file} does not match {IDENTITY_FILE}")
    return Signer(identity.site_id, identity.fingerprint, seed)


def reserve_seq(state_dir: Path, count: int = 1) -> int:
    """카운터에서 `count`개를 예약하고 첫 seq를 돌려준다(`site.json` 갱신 = fsync 뒤 반환). r4
    저널이 있으면 먼저 정리한다 — 저널이 카운터 예약 전에 끊겼어도 그 seq를 다시 내주지 않는다."""
    if count < 1:
        raise ValueError("count must be >= 1")
    settle_journal(state_dir)
    identity = _require_identity(state_dir)
    first = identity.next_report_seq
    save_identity(state_dir, replace(identity, next_report_seq=first + count))
    return first


def _reserve_through(state_dir: Path, last: int) -> None:
    """카운터가 `last` 이하이면 `last + 1`로 올린다(r4 저널이 예약 전에 끊긴 경우)."""
    identity = _require_identity(state_dir)
    if identity.next_report_seq <= last:
        save_identity(state_dir, replace(identity, next_report_seq=last + 1))


def pending(state_dir: Path) -> list[Path]:
    """대기 보고(이름순 = 시도 시작 시각순)."""
    queue = queue_dir(state_dir)
    return sorted(queue.glob("*.json")) if queue.is_dir() else []


def send_order(paths: Sequence[Path]) -> list[Path]:
    """온라인 전송 순서: 이미 seq가 있는 파일을 seq 순으로 먼저, 나머지는 이름순(리뷰 N4 — 시계가
    뒤로 가 새 파일 이름이 앞서도 송신 순서 = seq 순서). 읽을 수 없는 파일은 seq 없음으로 친다."""

    def key(path: Path) -> tuple[float, str]:
        try:
            seq = stored_seq(read_report(path))
        except (UnreadableReportError, ReportIOError):
            seq = None
        return (math.inf if seq is None else seq, path.name)

    return sorted(paths, key=key)


def read_report(path: Path) -> dict[str, Json]:
    """`ReportIOError`(읽기 실패 — 큐에 둔다)와 `UnreadableReportError`(내용이 깨짐 — reject)를
    구분한다(R2-L1)."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # ValueError 하위 — 읽었지만 내용이 깨짐
        raise UnreadableReportError(f"{path}: unreadable report ({exc})") from exc
    except OSError as exc:
        raise ReportIOError(f"{path}: cannot read report now ({exc})") from exc
    try:
        data: object = json.loads(text)
    except ValueError as exc:
        raise UnreadableReportError(f"{path}: unreadable report ({exc})") from exc
    if not isinstance(data, dict):
        raise UnreadableReportError(f"{path}: report is not a JSON object")
    # r4가 쓴 파일에는 서로게이트가 남아 있을 수 있다(`\\udcxx` 이스케이프) — 서명 전에 정리한다.
    return cast(dict[str, Json], storable(cast(dict[str, Json], data)))


def stored_seq(report: dict[str, Json]) -> int | None:
    value = report.get(SEQ_KEY)
    return value if type(value) is int and value >= 1 else None


def assign(state_dir: Path, path: Path) -> QueuedReport:
    """보고의 seq: 파일에 있으면 그 값, 없으면 예약 → 파일에 써 넣기(원자적 교체). 저널은 읽기 전에
    정리한다(예약 안의 정리가 이 파일에 저널 seq를 쓴 뒤 덮어쓰지 않게)."""
    settle_journal(state_dir)
    report = read_report(path)
    seq = stored_seq(report)
    if seq is None:
        seq = reserve_seq(state_dir)
        report = {**report, SEQ_KEY: seq}
        write_report_file(path, report)
    return QueuedReport(path, seq, report)


def move(path: Path, subdir: str) -> Path:
    """대기 보고를 `reports/<subdir>/`로 옮긴다(같은 이름이 있으면 덮어쓴다). 원본·대상 디렉터리
    모두 fsync한다(N3)."""
    target_dir = path.parent / subdir
    target_dir.mkdir(exist_ok=True)
    target = target_dir / path.name
    path.replace(target)
    fsync_dir(path.parent)
    fsync_dir(target_dir)
    return target


def reject(state_dir: Path, path: Path, reason: str) -> Path:
    """중앙이 영원히 받지 않을(또는 읽을 수 없는) 보고를 `rejected/`로 빼고 오류 로그·카운터를
    남긴다 — 큐 맨 앞을 막지 않게(파일은 보존)."""
    moved = move(path, REJECTED_DIR)
    counters = load_counters(state_dir)
    save_counters(state_dir, replace(counters, rejected_total=counters.rejected_total + 1))
    log.error("report %s rejected (%s) — moved to %s", path.name, reason, moved)
    return moved


def _load_journal(path: Path) -> tuple[int, list[str]] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EnrollError(f"{path}: unreadable export journal ({exc})") from exc
    doc = cast(dict[str, object], data) if isinstance(data, dict) else {}
    first, names = doc.get("first_seq"), doc.get("names")
    if type(first) is not int or first < 1 or not isinstance(names, list):
        raise EnrollError(f"{path}: malformed export journal")
    return first, [str(n) for n in cast(list[object], names)]


def settle_journal(state_dir: Path) -> None:
    """r4 저널의 seq를 아직 대기 중인 파일에 써 넣고 저널을 지운다(재실행 안전).

    저널에 든 파일을 지금 읽을 수 없으면(`ReportIOError`) 저널을 지우지 않고 그대로 올린다 —
    그 파일의 seq를 잃지 않게(R2-L1). 다음 tick이 다시 정리한다."""
    queue = queue_dir(state_dir)
    journal_file = queue / JOURNAL_FILE
    journal = _load_journal(journal_file)
    if journal is None:
        return
    first, names = journal
    _reserve_through(state_dir, first + len(names) - 1)
    for i, name in enumerate(names):
        path = queue / name
        if not path.exists():
            continue  # 이미 exported/ — r4가 그 묶음을 썼다
        try:
            report = read_report(path)
        except UnreadableReportError as exc:
            log.error("export journal: %s — left for the queue to reject", exc)
            continue
        if stored_seq(report) != first + i:
            write_report_file(path, {**report, SEQ_KEY: first + i})
    journal_file.unlink()
    fsync_dir(queue)


def contiguous_runs(reports: Sequence[QueuedReport]) -> list[list[QueuedReport]]:
    """seq 순으로 정렬해 빈틈없는 구간으로 나눈다(중앙 `parse_report_bundle`은 묶음 안 연속을
    요구한다)."""
    runs: list[list[QueuedReport]] = []
    for report in sorted(reports, key=lambda r: r.report_seq):
        if runs and report.report_seq == runs[-1][-1].report_seq + 1:
            runs[-1].append(report)
        else:
            runs.append([report])
    return runs


def export_reports(settings: Settings, out_dir: Path, *, now: datetime) -> tuple[Path, int] | None:
    """대기 보고의 첫 연속 구간을 site key 서명 묶음(`site-reports`)으로 `out_dir`에 쓰고 그 보고를
    `exported/`로 옮긴다. 대기 보고가 없으면 None — 호출자는 None이 될 때까지 부른다.

    seq는 파일에 저장된 값(온라인 전송이 먼저 매긴 것 포함)을 쓰고, 없는 파일만 새로 예약한다.
    묶음을 쓴 뒤 옮기기 전에 죽으면 다음 실행이 같은 seq로 같은 묶음을 다시 쓴다.
    """
    sign = signer(settings)
    state_dir = settings.state_dir
    settle_journal(state_dir)
    queued: list[QueuedReport] = []
    for path in pending(state_dir):
        try:
            queued.append(assign(state_dir, path))
        except UnreadableReportError as exc:
            reject(state_dir, path, str(exc))
        except ReportIOError as exc:
            log.warning("report export: %s — skipped, kept in the queue", exc)
    if not queued:
        return None
    run = contiguous_runs(queued)[0]
    first, last = run[0].report_seq, run[-1].report_seq
    payload: dict[str, Json] = {
        "site_id": sign.site_id,
        "key_fingerprint": sign.fingerprint,
        "exported_at": format_timestamp(now),
        "first_seq": first,
        "last_seq": last,
        "reports": [report.body for report in run],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"pureiron-reports-{sign.site_id}-{first:06d}-{last:06d}.json"
    atomic_write(target, dumps(sign_document(sign.seed, SitePurpose.REPORTS, payload)))
    for report in run:
        move(report.path, EXPORTED_DIR)
    return target, len(run)
