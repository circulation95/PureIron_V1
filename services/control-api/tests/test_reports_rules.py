"""seq 규칙과 보고 payload 형식(plan M4 C7): 종류별 수위보다 큰 seq만 반영, 나머지는 이력,
current(두 종류 공용)는 current_seq보다 큰 seq만, 형식 위반(열 범위 밖 정수·NUL·짝 없는
서로게이트) 400."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pureiron_bundle.sitesign import Json
from pureiron_control.platform.seqmarks import MessageKind, SeqMarks
from pureiron_control.reports import (
    Current,
    ReportFormatError,
    SeqOutcome,
    SeqVerdict,
    judge_seq,
    parse_heartbeat,
    parse_report,
    parse_report_bundle,
)

AT = "2026-11-01T09:00:00+00:00"
SEQ_MAX = 9_223_372_036_854_775_807  # PostgreSQL bigint 최댓값
GEN_MAX = 2_147_483_647  # PostgreSQL integer 최댓값

HB, REPORT = MessageKind.HEARTBEAT, MessageKind.REPORT
APPLIED, HISTORY = SeqOutcome.APPLIED, SeqOutcome.HISTORY


@pytest.mark.parametrize(
    ("marks", "current_seq", "kind", "report_seq", "expected"),
    [
        (SeqMarks(heartbeat=0, report=0), 0, REPORT, 1, SeqVerdict(APPLIED, True)),
        (SeqMarks(heartbeat=0, report=4), 4, REPORT, 5, SeqVerdict(APPLIED, True)),
        # 빈틈(전송 실패분)은 막지 않는다 — 늦게 오면 이력
        (SeqMarks(heartbeat=0, report=4), 4, REPORT, 9, SeqVerdict(APPLIED, True)),
        # 재전송(응답 유실)
        (SeqMarks(heartbeat=0, report=5), 5, REPORT, 5, SeqVerdict(HISTORY, False)),
        # 늦은 폐쇄망 반입
        (SeqMarks(heartbeat=0, report=5), 5, REPORT, 3, SeqVerdict(HISTORY, False)),
        (SeqMarks(heartbeat=5, report=0), 5, HB, 5, SeqVerdict(HISTORY, False)),
        (SeqMarks(heartbeat=5, report=0), 5, HB, 6, SeqVerdict(APPLIED, True)),
        # 보고 11이 끊겼다가 heartbeat 12 뒤에 도착 → 반영(heartbeat 수위에 가리지 않는다).
        # 다만 current는 heartbeat 12가 이미 썼으므로 바꾸지 않는다(성공 보고였어도).
        (SeqMarks(heartbeat=12, report=10), 12, REPORT, 11, SeqVerdict(APPLIED, False)),
        # 반대로 늦게 온 heartbeat는 보고 수위와 상관없이 자기 수위로만 판정한다.
        (SeqMarks(heartbeat=12, report=10), 12, HB, 11, SeqVerdict(HISTORY, False)),
        # 늦게 온 heartbeat 4는 자기 수위(3)로 반영되지만 성공 보고 12가 쓴 current는 그대로.
        (SeqMarks(heartbeat=3, report=12), 12, HB, 4, SeqVerdict(APPLIED, False)),
        # 보고 12가 실패라 current를 안 썼으면(current_seq 3) heartbeat 4가 current를 쓴다.
        (SeqMarks(heartbeat=3, report=12), 3, HB, 4, SeqVerdict(APPLIED, True)),
    ],
)
def test_seq_rule(
    marks: SeqMarks, current_seq: int, kind: MessageKind, report_seq: int, expected: SeqVerdict
) -> None:
    assert judge_seq(marks, current_seq, kind, report_seq) == expected


def test_seq_marks_advance_per_kind_and_next_seq_is_shared() -> None:
    marks = SeqMarks(heartbeat=12, report=10).advanced(REPORT, 11)
    assert marks == SeqMarks(heartbeat=12, report=11)
    assert marks.advanced(HB, 13) == SeqMarks(heartbeat=13, report=11)
    # 카운터는 공용 — rekey 다음 seq는 두 수위 중 큰 값 + 1.
    assert marks.next_seq == 13
    assert SeqMarks(heartbeat=2, report=7).next_seq == 8


def _report(seq: int, **overrides: Json) -> dict[str, Json]:
    report: dict[str, Json] = {
        "site_id": "site-a",
        "report_seq": seq,
        "generation": 3,
        "from": 2,
        "to": 3,
        "phase": "succeeded",
        "result": "succeeded",
        "code": None,
        "logs": "",
        "started_at": "2026-11-01T08:59:00+00:00",
        "ended_at": AT,
    }
    report.update(overrides)
    return report


def test_heartbeat_payload_is_parsed() -> None:
    beat = parse_heartbeat(
        {
            "site_id": "site-a",
            "report_seq": 7,
            "current": {"product": "2026.10.0", "project_version": "v3", "gen": 3},
            "updater_phase": "idle",
            "disk_state": "ok",
            "at": "2026-11-01T09:00:00Z",
        }
    )
    assert beat.report_seq == 7
    assert beat.current == Current("2026.10.0", "v3", 3)
    assert beat.at == datetime(2026, 11, 1, 9, tzinfo=UTC)
    assert (beat.updater_phase, beat.disk_state) == ("idle", "ok")


def test_column_range_edges_are_accepted() -> None:
    beat = parse_heartbeat({"report_seq": SEQ_MAX, "current": {"gen": GEN_MAX}, "at": AT})
    assert (beat.report_seq, beat.current) == (SEQ_MAX, Current(None, None, GEN_MAX))
    report = parse_report(_report(SEQ_MAX, generation=GEN_MAX, to=GEN_MAX, **{"from": 0}))
    assert (report.report_seq, report.generation, report.to) == (SEQ_MAX, GEN_MAX, GEN_MAX)


def test_heartbeat_without_applied_bundle_has_no_current() -> None:
    beat = parse_heartbeat({"report_seq": 1, "current": None, "at": AT})
    assert beat.current is None


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"at": AT}, "report_seq"),
        ({"report_seq": 0, "at": AT}, "report_seq"),
        ({"report_seq": True, "at": AT}, "report_seq"),
        ({"report_seq": "3", "at": AT}, "report_seq"),
        ({"report_seq": 1}, "at"),
        ({"report_seq": 1, "at": "2026-11-01T09:00:00"}, "at"),
        ({"report_seq": 1, "at": "yesterday"}, "at"),
        ({"report_seq": 1, "at": AT, "current": "g3"}, "current"),
        ({"report_seq": 1, "at": AT, "current": {"gen": "3"}}, "gen"),
        ({"report_seq": 1, "at": AT, "disk_state": 5}, "disk_state"),
        ({"report_seq": SEQ_MAX + 1, "at": AT}, "report_seq"),  # bigint 밖
        ({"report_seq": 1, "at": AT, "current": {"gen": GEN_MAX + 1}}, "gen"),  # integer 밖
        ({"report_seq": 1, "at": AT, "current": {"gen": -1}}, "gen"),
        ({"report_seq": 1, "at": AT, "updater_phase": "idle\u0000"}, "NUL"),  # jsonb 거부
        ({"report_seq": 1, "at": AT, "extra\u0000": 1}, "NUL"),
        # 짝 없는 서로게이트(JSON "\ud800")는 UTF-8로 저장할 수 없다 — 키도 같다.
        ({"report_seq": 1, "at": AT, "extra\udfff": 1}, "surrogate"),
    ],
)
def test_malformed_heartbeat_is_rejected(payload: dict[str, Json], match: str) -> None:
    with pytest.raises(ReportFormatError, match=match):
        parse_heartbeat(payload)


def test_report_payload_is_parsed() -> None:
    report = parse_report(_report(5))
    assert (report.report_seq, report.generation, report.to, report.result) == (
        5,
        3,
        3,
        "succeeded",
    )
    assert report.ended_at == datetime(2026, 11, 1, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"result": None}, "result"),
        ({"result": ""}, "result"),
        ({"to": "3"}, "to"),
        ({"ended_at": None}, "ended_at"),
        ({"to": GEN_MAX + 1}, "to"),
        ({"generation": GEN_MAX + 1}, "generation"),
        ({"from": GEN_MAX + 1}, "from"),
        ({"logs": "step 1\u0000step 2"}, "NUL"),  # 외부 명령 출력을 그대로 담은 로그
        ({"timeline": {"verifying": ["\u0000"]}}, "NUL"),
        # surrogateescape로 읽은 명령 출력(파일 이름 등)을 담은 로그
        ({"logs": "a\ud800b"}, "surrogate"),
    ],
)
def test_malformed_report_is_rejected(overrides: dict[str, Json], match: str) -> None:
    with pytest.raises(ReportFormatError, match=match):
        parse_report(_report(5, **overrides))


def test_rejected_bundle_with_unknown_generation_is_a_valid_report() -> None:
    """서명 전 거부(E_SIG 등)는 gen을 모른다 — Updater는 generation·to를 null로 보고한다."""
    report = parse_report(_report(2, generation=None, to=None, result="rejected"))
    assert (report.generation, report.to) == (None, None)


def _bundle(**overrides: Json) -> dict[str, Json]:
    bundle: dict[str, Json] = {
        "site_id": "site-a",
        "key_fingerprint": "ab" * 32,
        "exported_at": "2026-11-01T10:00:00Z",
        "first_seq": 4,
        "last_seq": 6,
        "reports": [_report(4), _report(5), _report(6)],
    }
    bundle.update(overrides)
    return bundle


def test_report_export_bundle_is_parsed_in_order() -> None:
    bundle = parse_report_bundle(_bundle())
    assert (bundle.site_id, bundle.first_seq, bundle.last_seq) == ("site-a", 4, 6)
    assert [r.report_seq for r in bundle.reports] == [4, 5, 6]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"reports": []}, "non-empty"),
        ({"reports": [_report(4), _report(6), _report(5)]}, "first_seq..last_seq"),
        ({"reports": [_report(4), _report(5)]}, "first_seq..last_seq"),
        ({"first_seq": 0}, "first_seq"),
        ({"key_fingerprint": None}, "key_fingerprint"),
        ({"reports": ["x"]}, "objects"),
        ({"first_seq": SEQ_MAX + 1}, "first_seq"),
        ({"reports": [_report(4, logs="\u0000"), _report(5), _report(6)]}, "NUL"),
    ],
)
def test_inconsistent_export_bundle_is_rejected(overrides: dict[str, Json], match: str) -> None:
    with pytest.raises(ReportFormatError, match=match):
        parse_report_bundle(_bundle(**overrides))
