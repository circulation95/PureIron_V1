"""G2 pytest 틀(M6-2 E7·E8, M6-3 H6·H12) — session fixture(prepare → 현장 VM 2대 → 설치 → 체크포인트
c0), 실행 순서(`harness.order` 표 — G2-1 → site-a 시나리오 → site-b 시나리오 → 외부 접속 0, 파일
이름 순서에 기대지 않는다)와 실패 분류(`failure_class` = infra·product)를 junit 속성으로 남긴다.

시나리오(`harness.scenarios`)는 현장 하나에 묶이고 c0 복원으로 시작한다 — 앞 시험이 남긴 VM 상태에
기대지 않으므로 nightly의 infra 재시도가 실패한 시험만 골라 새 pytest로 돌려도 된다. 판정 여러 개를
내는 시나리오는 module fixture가 한 번 돌리고 시험들이 항목별로 단언한다.

G2 시험(`test_g2_*.py`)은 `tests/e2e/gates/pytest.ini`로 실행할 때만 수집한다 — 루트 설정으로 도는
G1 `unit`(`pytest … tests/e2e`)에는 skip이 아니라 수집 자체가 빠진다(실제 VM이 필요하다).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import failures, g2, memwatch, order
from harness.context import E2EError

GATES_INI = Path(__file__).resolve().parent / "pytest.ini"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if collection_path.name.startswith("test_g2_") and config.inipath != GATES_INI:
        return True
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """G2 실행(`gates/pytest.ini`)이면 `harness.order` 표대로 시험 순서를 바꾼다. 표에 없는 시험
    파일은 수집 오류다."""
    if config.inipath != GATES_INI:
        return
    try:
        ranked = order.sort_order([item.path.name for item in items])
    except E2EError as exc:
        raise pytest.UsageError(str(exc)) from exc
    items[:] = [items[i] for i in ranked]


@pytest.fixture(scope="session")
def g2_run_id() -> str:
    """`PUREIRON_G2_RUN`(nightly가 정한 run id), 없으면 새 run id."""
    return os.environ.get("PUREIRON_G2_RUN") or g2.new_run_id()


@pytest.fixture(scope="session", autouse=True)
def windows_memory(g2_run_id: str) -> Generator[None]:
    """세션 내내 Windows 가용 메모리를 재서 최저값을 결과 디렉터리 `windows-memory.json`에 남긴다
    (B-2, `harness.memwatch`) — 시험을 시작하기 전(prepare 빌드 포함)부터 잰다."""
    watch = memwatch.MemoryWatch()
    watch.start()
    yield
    memwatch.finish(watch, lambda: g2.results_dir(g2_run_id) / memwatch.MEMORY_FILE)


@pytest.fixture(scope="session")
def g2_session(g2_run_id: str) -> g2.Session:
    """`PUREIRON_G2_SHA`(제품 커밋)가 없으면 저장소 HEAD를 쓴다."""
    session = g2.prepare(g2_run_id, os.environ.get("PUREIRON_G2_SHA") or g2.head_sha())
    g2.bring_up_all(session)
    return session


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """실패한 단계(setup·call)의 예외 종류로 분류해 junit `<properties>`에 남긴다."""
    if call.excinfo is not None and not call.excinfo.errisinstance(pytest.skip.Exception):
        item.user_properties.append(("failure_class", failures.classify(call.excinfo.value)))
    return (yield)
