"""G2-4 롤백(revert 전) + G2-6 ③ health 실패 — site-a. 같은 FAULT-h 시도 하나를 module fixture가
한 번 돌리고(`scenarios.rollback`: c0 복원 → FAULT-h g2 → 수렴 → NEXT g3 → 수렴) 시험 둘이 각자의
기준으로 본다(H7). 시험 하나만 골라 돌려도 fixture가 c0부터 다시 돈다."""

from __future__ import annotations

import pytest
from harness import g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def rollback(g2_session: g2.Session) -> scenarios.RollbackOutcome:
    return scenarios.rollback(g2_session)


def test_g2_4_rollback(rollback: scenarios.RollbackOutcome) -> None:
    """failed·E_HEALTH·BASE digest·rolling_back ≤ 300초·행 수 4종 ≥ 이전·site_rev 보존·BASE 트리와
    정의 복귀·경보 없음·메트릭 0·edit-lock 해제, 이어서 NEXT 재시도 succeeded·NEXT 트리·D9 ⓐⓑ·압축
    chunk 유지."""
    failed = scenarios.failed(rollback.rollback)
    failed += [f"retry.{name}" for name in scenarios.failed(rollback.retry)]
    assert not failed, failed


def test_g2_6_3_health_failure(rollback: scenarios.RollbackOutcome) -> None:
    """10분 안에 failed로 수렴·시도 잔류 없음·직전 정상 이미지로 ready·edit-lock 해제."""
    failed = scenarios.failed(rollback.health)
    assert not failed, failed
