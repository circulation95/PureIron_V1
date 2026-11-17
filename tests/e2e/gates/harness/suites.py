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
    # 평일(M6-4a P9, ★P5 개정, M6-4b-1 다시 셈): 첫 pytest 105분 — 4a 실측 45.5분 + 4b-1 fleet
    # 추정 약 23분 = 약 69분(4b 계획 3절 62분보다 크다: 빌드·PUT을 4a 실측 Windows 경로 값
    # 80·20초로 셌다). 4b-1 시나리오별(각자 c1 복원 1.5분 포함): G2-4 revert 약 9분(빌드·PUT 2회 +
    # FAULT-h 롤백 + BASE 적용), G2-6 ① 약 6.5분(`.part` 대기 ≤ 4분 상한·차단 ≤ 2분 상한 포함 —
    # 보통 각 1분 안), G2-W 약 6분(빌드·PUT 1회 + core 재시작 + GOOD 대기), G2-9 약 2.5분. 4b-2
    # (G2-7 약 5분)를 더해도 약 74분이라 값은 그대로 둔다(4b-1 실측 뒤 다시 맞춘다). 재시도
    # 45분(새 세션 prepare·VM 약 10분 + c1 준비 약 4분 + site-b 재시도 묶음 25~30분 — 3b 리뷰
    # L2), run 160분 = 105 + 복구 약 5 + 45 + preflight 여유 5, workflow 190분 = setup 10 + run
    # 160 + tail 15 + 여유 5. "run 90분"은 시간 제한이 아니라 실측 목표다 (넘으면 사용자 결정,
    # 계획 P11). M6-4b-2: 4b-1 실측(브랜치 dispatch) 평일 run 58분 + G2-7 약 5분(c0·중앙 c1 복원
    # 1분 + 빌드 1.5 + USB 만들기 0.3·hot-add·마운트 + apply 약 0.5 + 수렴 + export·꺼내기 0.5 +
    # 반입·읽기 0.3 — 4b 계획 3절) + site-b G2-1 USB 설치 약 0.5분(USB 만들기 약 20초, scp 대신) =
    # 약 63분 — 값은 그대로.
    "g2": Suite(
        "g2",
        "g2 and not weekly",
        workflow_s=190 * 60,
        run_budget_s=160 * 60,
        pytest_timeout_s=105 * 60,
        retry_timeout_s=45 * 60,
        retry_min_s=10 * 60,
    ),
    # 주간(추정 — 첫 주간 실측으로 다시 맞춘다): crash 한 건 5~7분(c0 복원·메모리 대기 0~2분 +
    # 번들 빌드 약 16초 + 창 대기 150초 + phase 진행 + 주입 뒤 수렴) x 18건 = 90~126분, 나머지
    # 평일 몫(prepare·VM 2대·G2-1·4·5·6·8·외부 접속 0) 35~45분 → 합 125~171분. 첫 pytest 200분(추정
    # 상한의 약 1.17배 — E10 재시도는 한 건에 5~7분을 더한다). 넘기면 junit이 없어 그 주 결과가
    # `pytest` 한 줄로만 남으므로 첫 pytest에 몫을 몰고 재시도는 40분(새 세션 prepare·VM 한 대 약
    # 10분 + crash 몇 건), run 240분 = 270 - setup 10 - tail 15 - 여유 5. M6-4b-1 뒤에도 그대로:
    # 4a 전 주간 99분 + fleet 4a 약 7분 + 4b-1 약 23분 = 약 129분(+ 4b-2 5분 = 134분) < 첫 pytest
    # 200분, 재시도 40분은 남은 run 예산(240 - 134 = 106분) 안에 든다. 4b-1 주간 실측 1시간 54분 +
    # 4b-2 약 5.5분 = 약 120분.
    "weekly": Suite(
        "weekly",
        "g2",
        workflow_s=270 * 60,
        run_budget_s=240 * 60,
        pytest_timeout_s=200 * 60,
        retry_timeout_s=40 * 60,
        retry_min_s=10 * 60,
    ),
}


def from_env(environ: Mapping[str, str]) -> Suite:
    """`PUREIRON_G2_SUITE`(빈 값·없음 = `g2`)의 묶음. 모르는 이름은 `E2EError`."""
    name = environ.get(SUITE_ENV) or DEFAULT_SUITE
    if name not in SUITES:
        raise E2EError(f"{SUITE_ENV} must be one of {sorted(SUITES)}, got {name!r}")
    return SUITES[name]
