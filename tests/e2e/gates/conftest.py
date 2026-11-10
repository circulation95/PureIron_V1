"""G2 pytest 틀(M6-2 E7·E8) — session fixture(prepare → 현장 VM 2대 → 설치 → 체크포인트 c0)와
실패 분류(`failure_class` = infra·product)를 junit 속성으로 남긴다.

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

from harness import failures, g2

GATES_INI = Path(__file__).resolve().parent / "pytest.ini"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if collection_path.name.startswith("test_g2_") and config.inipath != GATES_INI:
        return True
    return None


@pytest.fixture(scope="session")
def g2_session() -> g2.Session:
    """`PUREIRON_G2_RUN`(nightly가 정한 run id)·`PUREIRON_G2_SHA`(제품 커밋)가 없으면 새 run id와
    저장소 HEAD를 쓴다."""
    run_id = os.environ.get("PUREIRON_G2_RUN") or g2.new_run_id()
    session = g2.prepare(run_id, os.environ.get("PUREIRON_G2_SHA") or g2.head_sha())
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
