"""G2-9 중앙 화면 stale(site-b) — fleet 묶음. site-b·중앙을 c1으로 되돌린 뒤 VM nft로 중앙을 막고
control-web과 같은 경로(Caddy basic auth → `/ui/fleet`)로 stale → 해제 → online을
`fleet_scenarios.fleet_view`가 한 번 돌리고 항목별로 단언한다. 화면 규칙("최신" 배지)은 CI
`fleet.spec.ts`가 본다(★Q2)."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def outcome(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.fleet_view(g2_session, fleet_c1)


@pytest.mark.parametrize("item", fleet_scenarios.G2_9_ITEMS)
def test_g2_9_fleet_view(outcome: dict[str, bool], item: str) -> None:
    assert outcome.get(item) is True, scenarios.failed(outcome)
