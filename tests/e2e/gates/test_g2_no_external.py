"""외부 접속 0(맨 끝, 현장별) — 설치 이후 모든 시나리오를 거친 누적 카운터(nft, 복원 직전마다
표시해 더한다)가 0이고 카운터를 잃은 복원이 없다. `field_fwd`(선언된 장비 폴링)는 따로 센다.
실행 순서는 `harness.order`가 맨 끝으로 고정한다."""

from __future__ import annotations

import pytest
from harness import g2

pytestmark = pytest.mark.g2


@pytest.mark.parametrize("site_id", ["site-a", "site-b"])
def test_g2_no_external(g2_session: g2.Session, site_id: str) -> None:
    checks = g2.check_no_external(g2_session, site_id)
    failed = sorted(name for name, ok in checks.items() if not ok)
    assert not failed, f"{site_id}: {failed} (nft_total {g2_session.sites[site_id].ctx.nft_total})"
