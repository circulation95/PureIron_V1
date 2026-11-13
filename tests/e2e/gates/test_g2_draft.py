"""G2-8 드래프트 회수(PR 전) — site-b. PATCH 3회 → 미흡수 repo 번들 `E_DRAFT` → export·absorb·gate →
흡수 트리 재적용 → FAULT-h 롤백(site_rev·base_site_rev·PATCH·트리 유지)을 module fixture가 한 번
돌리고 항목별로 단언한다(`draft.draft_recall`)."""

from __future__ import annotations

import pytest
from harness import draft, g2, scenarios

pytestmark = pytest.mark.g2


@pytest.fixture(scope="module")
def recall(g2_session: g2.Session) -> dict[str, dict[str, bool]]:
    return draft.draft_recall(g2_session)


@pytest.mark.parametrize("item", draft.ITEMS)
def test_g2_8_draft_recall(recall: dict[str, dict[str, bool]], item: str) -> None:
    failed = scenarios.failed(recall[item])
    assert not failed, failed
