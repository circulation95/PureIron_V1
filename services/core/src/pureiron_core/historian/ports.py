"""다른 모듈이 historian에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HistorianStatus:
    """`/readyz.historian`(준비 판정에는 쓰지 않음). 누적 값은 core 기동 후 기준.

    - `buffer_rows`: 아직 DB에 쓰지 못한 메모리 버퍼 행 수(쓰는 중인 행 포함).
    - `dropped_total`: 버퍼 상한을 넘어 버린(가장 오래된) 행 수.
    - `written_total`: INSERT에 넘긴 행 수(중복으로 무시된 행 포함).
    - `poisoned_total`: site-db가 행 내용 때문에 거부해 반분 격리 끝에 버린 행 수(R1 L9b).
    - `write_lag_s`: 버퍼에서 가장 오래 기다린 행의 대기 시간(버퍼가 비면 None).
    - `db_ok`: 마지막 쓰기 성공 여부.
    - `rejected_total`: 이력에서 버린 샘플 = batch `plan_rev`가 현재 rev보다 앞선 것(DB 재생성
      신호) + 현재 정의에 없는 tag_id의 live 샘플(정의 기준 — plan에서 문제로 빠진 태그는 기록)
      + int4 범위 밖 tag_id.
    """

    buffer_rows: int
    dropped_total: int
    written_total: int
    poisoned_total: int
    write_lag_s: float | None
    db_ok: bool
    rejected_total: int


class HistorianStatusSource(Protocol):
    def status(self) -> HistorianStatus: ...
