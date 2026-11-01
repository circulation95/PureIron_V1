"""report_seq 반영 수위(plan M4 C7): 카운터는 heartbeat·보고 공용, 수위는 종류별.

`reports`(seq 규칙)와 `sites`(rekey 다음 seq)가 같은 값을 쓴다 — 도메인 모듈끼리 import하지 않게
여기 둔다(import-linter 계약 (c)).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MessageKind(StrEnum):
    HEARTBEAT = "heartbeat"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class SeqMarks:
    """종류별 반영 수위(그 종류에서 마지막으로 반영한 `report_seq`)."""

    heartbeat: int
    report: int

    def of(self, kind: MessageKind) -> int:
        return self.heartbeat if kind is MessageKind.HEARTBEAT else self.report

    def advanced(self, kind: MessageKind, report_seq: int) -> SeqMarks:
        if kind is MessageKind.HEARTBEAT:
            return SeqMarks(heartbeat=report_seq, report=self.report)
        return SeqMarks(heartbeat=self.heartbeat, report=report_seq)

    @property
    def next_seq(self) -> int:
        """카운터는 공용이므로 다음 seq는 두 수위 중 큰 값 + 1(rekey)."""
        return max(self.heartbeat, self.report) + 1
