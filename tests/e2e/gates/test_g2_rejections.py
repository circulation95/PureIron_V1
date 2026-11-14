"""G2-5 거부 — site-b. 설치 전 변조 2건(부팅 직후 실제 설치 전에 돈 탐침의 판정)과, c0에서 거부 6건
→ NEXT g2 → 거부 2건(`rejects.rejections`, module fixture가 한 번 돈다)을 항목별로 단언한다.
거부마다 종료 코드·거부 코드·core StartedAt·실행 이미지·edit-lock·applied gen. `E_DRAFT`는
`test_g2_draft.py`."""

from __future__ import annotations

import pytest
from harness import g2, preinstall, rejects, scenarios

pytestmark = pytest.mark.g2


@pytest.mark.parametrize("probe", list(preinstall.PROBES))
def test_g2_5_tampered_before_install(g2_session: g2.Session, probe: str) -> None:
    """① 변조 bootstrap-verify.sh → runbook sha256 대조에서 멈춤 ② 변조 deb → 정품 스크립트가
    payload 대조에서 실패. 둘 다 docker 미설치·탐침 디렉터리 삭제."""
    failed = scenarios.failed(rejects.check_preinstall(g2_session, probe))
    assert not failed, failed


@pytest.fixture(scope="module")
def rejections(g2_session: g2.Session) -> dict[str, dict[str, bool]]:
    return rejects.rejections(g2_session)


@pytest.mark.parametrize("item", rejects.ITEMS)
def test_g2_5_rejected(rejections: dict[str, dict[str, bool]], item: str) -> None:
    failed = scenarios.failed(rejections[item])
    assert not failed, failed
