"""G2-1 — 두 현장 각각 설치 성공, 실행 이미지 == 기대 digest(site-a BASE, site-b NEXT), 5분 안에
100태그 전부 GOOD, 장비 health connected, 외부 접속 0."""

from __future__ import annotations

import pytest
from harness import g2

pytestmark = pytest.mark.g2


@pytest.mark.parametrize("site_id", ["site-a", "site-b"])
def test_g2_1_install_and_100_tags_good(g2_session: g2.Session, site_id: str) -> None:
    checks = g2.check_install(g2_session, site_id)
    failed = sorted(name for name, ok in checks.items() if not ok)
    assert not failed, f"{site_id} ({g2.site_release(g2_session.pair, site_id)}): {failed}"
