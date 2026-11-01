"""seq 규칙과 보고 payload 형식(plan M4 C7): seq > last_seq만 반영, 나머지는 이력, 형식 위반 400."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pureiron_bundle.sitesign import Json
from pureiron_control.reports import (
    Current,
    ReportFormatError,
    SeqOutcome,
    judge_seq,
    parse_heartbeat,
    parse_report,
    parse_report_bundle,
)

AT = "2026-11-01T09:00:00+00:00"


@pytest.mark.parametrize(
    ("last_seq", "report_seq", "expected"),
    [
        (0, 1, SeqOutcome.APPLIED),
        (4, 5, SeqOutcome.APPLIED),
        (4, 9, SeqOutcome.APPLIED),  # 빈틈(전송 실패분)은 막지 않는다 — 늦게 오면 이력
        (5, 5, SeqOutcome.HISTORY),  # 같은 seq 재전송(응답 유실)
        (5, 3, SeqOutcome.HISTORY),  # 늦게 온 폐쇄망 반입
        (5, 1, SeqOutcome.HISTORY),
    ],
)
def test_seq_rule(last_seq: int, report_seq: int, expected: SeqOutcome) -> None:
    assert judge_seq(last_seq, report_seq) is expected


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
    ],
)
def test_inconsistent_export_bundle_is_rejected(overrides: dict[str, Json], match: str) -> None:
    with pytest.raises(ReportFormatError, match=match):
        parse_report_bundle(_bundle(**overrides))
