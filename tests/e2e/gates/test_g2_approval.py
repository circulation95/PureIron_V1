"""E_APPROVAL(K14) — fleet 묶음 맨 앞. 중앙만 c1으로 되돌리고(VM 없음) 승인 없이 merge한 PR의
publish가 거부되는지(run 실패·로그 `E_APPROVAL`·중앙 max·사슬 불변), main을 되돌린 publish가 게시
0으로 끝나는지 본다(`fleet_scenarios.e_approval`). module fixture가 한 번 돌리고 항목별로
단언한다."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2

ITEMS = (
    "main_matches_c1",
    "run_failed",
    "e_approval_logged",
    "not_signed",
    "max_published_unchanged",
    "chain_unchanged",
    "main_reverted",
    "revert_published_nothing",
)


@pytest.fixture(scope="module")
def approval(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.e_approval(g2_session, fleet_c1)


@pytest.mark.parametrize("item", ITEMS)
def test_e_approval(approval: dict[str, bool], item: str) -> None:
    assert approval.get(item) is True, scenarios.failed(approval)
