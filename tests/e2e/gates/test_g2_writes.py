"""G2-W 현장 쓰기 가드레일(site-b) — fleet 묶음. site-b·중앙을 c1으로 되돌린 뒤 스위치 꺼짐 거부 →
`writes.enabled` 번들 게시 → 선언 태그 쓰기(CONFIRMED·readback·감사) → 미선언 태그 거부를
`fleet_scenarios.writes`가 한 번 돌리고 항목별로 단언한다."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def outcome(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.writes(g2_session, fleet_c1)


@pytest.mark.parametrize("item", fleet_scenarios.G2_W_ITEMS)
def test_g2_w_writes(outcome: dict[str, bool], item: str) -> None:
    assert outcome.get(item) is True, scenarios.failed(outcome)
