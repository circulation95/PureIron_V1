"""G2 nightly 묶음(suite, M6-3 H11·★H13) — 무엇을 돌리고 얼마나 기다리는지. 순수 데이터다.

- `g2`(평일): `-m "g2 and not weekly"` — G2-1·롤백·거부·fault·draft·crash 2건·외부 접속 0.
- `weekly`(일요일, Windows 작업 스케줄러가 `-f suite=weekly`로 부른다): `-m g2` — 평일 시험 전부 +
  crash 16건(표지 `weekly`).

시간 예산은 묶음마다다. workflow `timeout-minutes`(`nightly.yml`)는 가장 긴 묶음(weekly) 값
하나이고, 실제 시간 제한은 `nightly.py`가 묶음별로 건다 — `run_budget_s`(preflight 시작부터 첫
실행·복구·재시도 끝까지) + checkout·`uv sync`(`SETUP_RESERVE_S`) +
collect·publish·cleanup(`TAIL_RESERVE_S`)이 `workflow_s` 안에 들어야 status·이력이 남는다. 입력은
환경변수 `PUREIRON_G2_SUITE`(없으면 `g2`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from harness.context import E2EError

SUITE_ENV = "PUREIRON_G2_SUITE"
DEFAULT_SUITE = "g2"


@dataclass(frozen=True)
class Suite:
    name: str
    marker: str
    """pytest `-m` 식."""
    workflow_s: int
    """이 묶음이 끝나야 하는 workflow 시간(Actions job 한도는 가장 긴 묶음의 값)."""
    run_budget_s: int
    pytest_timeout_s: int
    """첫 pytest 한 번의 시간 제한(`run_budget_s` 남은 시간으로 더 줄어든다)."""
    retry_timeout_s: int
    retry_min_s: int
    """재시도에 이만큼도 남지 않으면 건너뛴다."""


SUITES: dict[str, Suite] = {
    # 평일: 지금 값 그대로(M6-2 — workflow 120분 안에 setup 10분 + run 90분 + tail 15분).
    "g2": Suite(
        "g2",
        "g2 and not weekly",
        workflow_s=120 * 60,
        run_budget_s=90 * 60,
        pytest_timeout_s=75 * 60,
        retry_timeout_s=30 * 60,
        retry_min_s=10 * 60,
    ),
    # 주간: 추정 평일 묶음 약 35분 + crash 16건 약 70분 = 약 105분. 첫 pytest 150분(추정의 약
    # 1.4배 — c0 복원 대기·E10 재시도 몫), 재시도 60분(새 세션 prepare·VM 한 대 부팅 약 10분 +
    # crash 몇 건 — 재시도는 실패한 시험과 그 현장 묶음만 돈다), run 180분 = 210 - setup 10 - tail
    # 15 - 여유 5.
    "weekly": Suite(
        "weekly",
        "g2",
        workflow_s=210 * 60,
        run_budget_s=180 * 60,
        pytest_timeout_s=150 * 60,
        retry_timeout_s=60 * 60,
        retry_min_s=10 * 60,
    ),
}


def from_env(environ: Mapping[str, str]) -> Suite:
    """`PUREIRON_G2_SUITE`(빈 값·없음 = `g2`)의 묶음. 모르는 이름은 `E2EError`."""
    name = environ.get(SUITE_ENV) or DEFAULT_SUITE
    if name not in SUITES:
        raise E2EError(f"{SUITE_ENV} must be one of {sorted(SUITES)}, got {name!r}")
    return SUITES[name]
