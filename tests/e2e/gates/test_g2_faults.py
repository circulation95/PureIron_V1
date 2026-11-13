"""G2-6 fault — ② 마이그레이션 실패(site-b, c0 = NEXT): FAULT-m(NEXT core + 실패하는 migration,
epoch + 1)을 적용하면 `failed`·`E_MIGRATE`로 끝나고 NEXT 이미지·epoch·스키마 이름 집합 5종이 그대로
(표지 테이블 `e2e_fault_marker` 없음)이며 행 수가 줄지 않는다(`scenarios.migrate_failure`). ③은
G2-4와 같은 시도라 `test_g2_rollback.py`에 있다. ④⑤(crash)는 M6-3b."""

from __future__ import annotations

import pytest
from harness import g2, scenarios

pytestmark = pytest.mark.g2


def test_g2_6_2_migrate_failure(g2_session: g2.Session) -> None:
    failed = scenarios.failed(scenarios.migrate_failure(g2_session))
    assert not failed, failed
