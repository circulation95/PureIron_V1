"""G2-6 ④⑤·주간 G2-X crash — site-a. 9 phase x kill·reset = 18건, 매 건 c0 복원 → BASE→NEXT
(`rolling_back`은 BASE→FAULT-h, `fetched`는 CLI 대상)에 주입하고 10분 안 수렴을 본다
(`crash.run_case`). 평일 묶음은 `reset@switching`·`kill@migrating` 2건, 나머지 16건은 표지
`weekly`."""

from __future__ import annotations

import pytest
from harness import crash, g2, scenarios

pytestmark = pytest.mark.g2

CASES = [
    pytest.param(case, id=case.id, marks=[pytest.mark.weekly] if case.weekly else [])
    for case in crash.PLAN
]


@pytest.mark.parametrize("case", CASES)
def test_g2_6_crash(g2_session: g2.Session, case: crash.CrashCase) -> None:
    failed = scenarios.failed(crash.run_case(g2_session, case))
    assert not failed, failed
