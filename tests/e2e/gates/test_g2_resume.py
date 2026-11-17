"""G2-6 ① 내려받기 이어받기(site-a) — fleet 묶음(c0 묶음의 `test_g2_faults.py`가 아니다). site-a·
중앙을 c1으로 되돌린 뒤 VM nft로 중앙 응답을 느리게 → NEXT 게시 → `.part`가 기준을 넘으면 전부
차단 → Updater 실패 로그·`.part` 유지 → 해제 → 이어받기(해제 뒤 받은 바이트)·적용을
`fleet_scenarios.resume`이 한 번 돌리고 항목별로 단언한다."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def outcome(g2_session: g2.Session, fleet_c1: fleet_session.FleetSession) -> dict[str, bool]:
    return fleet_scenarios.resume(g2_session, fleet_c1)


@pytest.mark.parametrize("item", fleet_scenarios.G2_6_1_ITEMS)
def test_g2_6_1_resume(outcome: dict[str, bool], item: str) -> None:
    assert outcome.get(item) is True, scenarios.failed(outcome)
