"""G2-4 revert(중앙 경로, site-a) — fleet 묶음. site-a·중앙을 c1으로 되돌린 뒤 FAULT-h g2를 중앙에
직접 PUT → 자동 롤백(failed·E_HEALTH·BASE) → BASE g3 PUT → succeeded·applied 3·BASE·중앙 current 3을
module fixture가 한 번 돌리고(`fleet_scenarios.revert`) 항목별로 단언한다. c0 경로의 롤백(revert
전)은 `test_g2_rollback.py`."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def outcome(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.revert(g2_session, fleet_c1)


@pytest.mark.parametrize("item", fleet_scenarios.G2_4_REVERT_ITEMS)
def test_g2_4_revert(outcome: dict[str, bool], item: str) -> None:
    assert outcome.get(item) is True, scenarios.failed(outcome)
