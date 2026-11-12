"""G2 시험 실행 순서(M6-3 H12) — 파일 이름 순서에 기대지 않고 이 표 하나가 정한다.

G2-1(설치) → site-a 시나리오 → site-b 시나리오 → 외부 접속 0(현장별, 맨 끝). 한 파일 안에서는
정의 순서 그대로다. 표에 없는 `test_g2_*.py`는 수집에서 멈춘다 — 새 시나리오 파일은 여기에 자리를
잡아야 돈다. `conftest.pytest_collection_modifyitems`가 부른다.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.context import E2EError

FIRST = "G2-1"
LAST = "last"
FILES: tuple[tuple[str, str], ...] = (
    ("test_g2_install.py", FIRST),  # G2-1 두 현장 설치·100태그
    # ---- site-a(BASE, c0 = 태그가 있는 최신 r*) — BASE→NEXT 방향이 필요한 것
    ("test_g2_rollback.py", "site-a"),  # G2-4(revert 전) + G2-6 ③ health 실패
    # 3b: ("test_g2_crash.py", "site-a") — G2-6 ④⑤·주간 crash
    # ---- site-b(NEXT, c0 = HEAD) — HEAD의 PATCH·export·검증 코드를 보는 것
    ("test_g2_faults.py", "site-b"),  # G2-6 ② migrate 실패
    # 3b: ("test_g2_rejections.py", "site-b") — G2-5, ("test_g2_draft.py", "site-b") — G2-8
    # ---- 맨 끝: 모든 시나리오를 거친 누적 카운터
    ("test_g2_no_external.py", LAST),
)
"""(시험 파일, 묶음) — 이 순서대로 돈다."""
GROUPS = (FIRST, "site-a", "site-b", LAST)


def rank(file_name: str) -> int:
    """`file_name`의 실행 순위(`FILES` 위치). 묶음 순서(`GROUPS`)를 어기는 표는 멈춘다."""
    names = [name for name, _ in FILES]
    if file_name not in names:
        raise E2EError(f"{file_name} has no place in harness/order.py FILES")
    return names.index(file_name)


def check_table(files: Sequence[tuple[str, str]] = FILES) -> None:
    """표가 묶음 순서대로이고(G2-1 → site-a → site-b → 맨 끝) 맨 끝 묶음이 마지막인지."""
    groups = [GROUPS.index(group) for _, group in files]
    if groups != sorted(groups) or not files or files[-1][1] != LAST:
        raise E2EError(f"harness/order.py FILES out of order: {[g for _, g in files]}")


def sort_order(file_names: Sequence[str]) -> list[int]:
    """수집된 시험(각자의 파일 이름, 수집 순서)을 실행 순서로 — 원래 위치의 목록. 같은 파일 안은
    수집 순서 그대로다(안정 정렬)."""
    check_table()
    return sorted(range(len(file_names)), key=lambda i: rank(file_names[i]))
