"""G2-7 폐쇄망(site-b c0 + 중앙 c1 DB) — fleet 묶음 맨 끝. VM nft로 중앙 쪽 ssh 외 전부 차단 →
USB 대용 디스크로 NEXT gen+1 적용 → `report export`를 같은 디스크로 꺼냄 → 호스트 `POST
/offline-imports` → 중앙 `/fleet`·control-web 경로 반영을 `fleet_scenarios.airgap`이 한 번 돌리고
항목별로 단언한다."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def outcome(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.airgap(g2_session, fleet_c1)


@pytest.mark.parametrize("item", fleet_scenarios.G2_7_ITEMS)
def test_g2_7_airgap(outcome: dict[str, bool], item: str) -> None:
    assert outcome.get(item) is True, scenarios.failed(outcome)
