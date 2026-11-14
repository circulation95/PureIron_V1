"""G2-2 예약 갱신(site-a) + G2-3 격리(site-b) — fleet 묶음. 두 현장·중앙을 c1으로 되돌린 뒤 bot PR
(site-a gen+1 NEXT, 1회 창 now+3분) → 승인 → merge → publish → 창 → 적용을 module fixture가 한 번
돌리고(`fleet_scenarios.scheduled`) 항목별로 단언한다."""

from __future__ import annotations

import pytest
from harness import fleet_scenarios, fleet_session, g2, scenarios

pytestmark = pytest.mark.g2

G2_2 = (
    "verified_before_window",
    "locked_in_window",
    "locked_to_succeeded_120s",
    "succeeded",
    "images_next",
    "fleet_current",
    "publish_success",
    "published_site-a",
    "untouched_site-b",
    "publish_no_failure",
)
G2_3 = (
    "images_unchanged",
    "content_hash_unchanged",
    "applied_gen_unchanged",
    "core_started_at_unchanged",
    "not_published",
)


@pytest.fixture(scope="module")
def scheduled(
    g2_session: g2.Session, fleet_c1: fleet_session.FleetSession
) -> fleet_scenarios.ScheduledOutcome:
    return fleet_scenarios.scheduled(g2_session, fleet_c1)


@pytest.mark.parametrize("item", G2_2)
def test_g2_2_scheduled_update(scheduled: fleet_scenarios.ScheduledOutcome, item: str) -> None:
    assert scheduled.g2_2.get(item) is True, scenarios.failed(scheduled.g2_2)


@pytest.mark.parametrize("item", G2_3)
def test_g2_3_isolation(scheduled: fleet_scenarios.ScheduledOutcome, item: str) -> None:
    assert scheduled.g2_3.get(item) is True, scenarios.failed(scheduled.g2_3)
