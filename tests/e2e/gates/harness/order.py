"""G2 시험 실행 순서(M6-3 H12) — 파일 이름 순서에 기대지 않고 이 표 하나가 정한다.

G2-1(설치) → site-a 시나리오 → site-b 시나리오 → 외부 접속 0(현장별, 맨 끝). 한 파일 안에서는
정의 순서 그대로다. 표에 없는 `test_g2_*.py`는 수집에서 멈춘다 — 새 시나리오 파일은 여기에 자리를
잡아야 돈다. `conftest.pytest_collection_modifyitems`가 부른다.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------- infra 재시도 묶음(리뷰 M1)

SITE_PARAM = re.compile(r"\[([^\]]+)\]$")
"""맨 끝 묶음 시험 id의 현장 매개변수(`test_g2_no_external[site-a]` → `site-a`)."""


def _site_of(test_id: str) -> tuple[str, str] | None:
    """시험 id(`<파일>::<이름>`)의 (묶음 종류, 현장): 현장 묶음이면 ("scenario", 현장), 맨 끝
    묶음이면 ("last", 매개변수 현장), 그 밖(G2-1·표에 없음)은 None."""
    file_name, _, name = test_id.partition("::")
    group = dict(FILES).get(file_name)
    if group == LAST:
        found = SITE_PARAM.search(name)
        return ("last", found[1]) if found else None
    if group is not None and group not in (FIRST, LAST):
        return ("scenario", group)
    return None


def retry_closure(targets: Sequence[str], every: Sequence[str]) -> list[str]:
    """infra 재시도 대상 묶기 — 재시도는 새 세션(새 VM, c0 새로)이라 외부 접속 0의 뜻이 바뀐다.
    처음 대상(`targets`) 기준으로 한 번만 더한다(연쇄하지 않는다): 한 현장의 시나리오 시험이
    대상이면 그 현장의 맨 끝 시험(외부 접속 0)을 더하고(다시 돈 시나리오의 외부 접속을 본다), 맨 끝
    시험이 대상이면 그 현장의 시나리오 시험 전부를 더한다(시나리오 없는 세션의 카운터로 통과하지
    않게). 현장은 `FILES` 표에서 읽는다 — 시험을 더하면 묶음이 따라온다. `every`(첫 실행의 시험 id
    전부)에서 고르고 그 순서로 돌려준다."""
    found = [f for t in targets if (f := _site_of(t)) is not None]
    want = {("last" if kind == "scenario" else "scenario", site) for kind, site in found}
    chosen = set(targets) | {t for t in every if _site_of(t) in want}
    return [t for t in every if t in chosen] + [t for t in targets if t not in every]
