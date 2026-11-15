"""G2 시험 실행 순서(M6-3 H12) — 파일 이름 순서에 기대지 않고 이 표 하나가 정한다.

G2-1(설치) → site-a 시나리오 → site-b 시나리오 → fleet(중앙 직접 게시, 현장 둘 — c1 쌍) →
crash(site-a, 주간 16건이 길어 뒤로) → 외부 접속 0(현장별, 맨 끝). 한 파일 안에서는 정의 순서
그대로다. 표에 없는 `test_g2_*.py`는 수집에서 멈춘다 — 새 시나리오 파일은 여기에 자리를
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
    # ---- site-b(NEXT, c0 = HEAD) — HEAD의 PATCH·export·검증 코드를 보는 것
    ("test_g2_rejections.py", "site-b"),  # G2-5 거부(설치 전 변조 2건 + c0·g2 거부 8건)
    ("test_g2_faults.py", "site-b"),  # G2-6 ② migrate 실패
    ("test_g2_draft.py", "site-b"),  # G2-8 드래프트 회수(PR 전) + 그 뒤 롤백
    # ---- fleet(c1 쌍 — 중앙 + 두 현장 온라인, M6-4a). GitHub 승인 경로(E_APPROVAL 등)는 nightly
    # 밖 — 릴리스 마감 fleet 리허설(★P5)
    ("test_g2_scheduled.py", "fleet"),  # G2-2 예약 갱신(site-a) + G2-3 격리(site-b)
    ("test_g2_revert.py", "fleet"),  # G2-4 revert(site-a, FAULT-h → 롤백 → BASE 다시 게시)
    ("test_g2_resume.py", "fleet"),  # G2-6 ① 내려받기 이어받기(site-a)
    ("test_g2_writes.py", "fleet"),  # G2-W 현장 쓰기 가드레일(site-b)
    ("test_g2_fleet_view.py", "fleet"),  # G2-9 중앙 화면 stale → online(site-b)
    # ---- crash(site-a) — G2-6 ④⑤ 2건 + 주간 16건(`weekly`), 매 건 c0 복원
    ("test_g2_crash.py", "crash"),
    # ---- 맨 끝: 모든 시나리오를 거친 누적 카운터
    ("test_g2_no_external.py", LAST),
)
"""(시험 파일, 묶음) — 이 순서대로 돈다."""
CRASH = "crash"
FLEET = "fleet"
GROUPS = (FIRST, "site-a", "site-b", FLEET, CRASH, LAST)
GROUP_SITES: dict[str, tuple[str, ...]] = {
    "site-a": ("site-a",),
    "site-b": ("site-b",),
    FLEET: ("site-a", "site-b"),
    CRASH: ("site-a",),
}
"""시나리오 묶음 → 그 묶음이 쓰는 현장. fleet은 c1 쌍이 두 현장을 함께 온라인으로 만든다."""
CENTRAL_GROUPS = frozenset({FLEET})
"""중앙(c1 쌍)이 필요한 묶음 — 이 묶음 시험이 하나도 없으면 중앙을 띄우지 않는다."""


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


def _sites_of(test_id: str) -> tuple[str, frozenset[str]] | None:
    """시험 id(`<파일>::<이름>`)의 (묶음 종류, 현장들): 시나리오 묶음이면 ("scenario", 그 묶음의
    현장들), 맨 끝 묶음이면 ("last", {매개변수 현장}), 그 밖(G2-1·표에 없음)은 None."""
    file_name, _, name = test_id.partition("::")
    group = dict(FILES).get(file_name)
    if group == LAST:
        found = SITE_PARAM.search(name)
        return ("last", frozenset({found[1]})) if found else None
    if group in GROUP_SITES:
        return ("scenario", frozenset(GROUP_SITES[group]))
    return None


SITES = ("site-a", "site-b")


def sites_needed(test_ids: Sequence[str]) -> set[str]:
    """선택된 시험(`<파일>::<이름>`)이 쓰는 현장 — 세션 fixture가 이 현장만 올린다(3a 리뷰 L3: crash
    재시도가 site-b까지 띄우지 않게). 시나리오 묶음은 `GROUP_SITES`, G2-1·맨 끝 묶음은 매개변수
    현장(`[site-a]`)이다. 현장을 정할 수 없는 시험(표에 없음·매개변수 없음)이 하나라도 있으면 둘
    다."""
    needed: set[str] = set()
    for test_id in test_ids:
        file_name, _, name = test_id.partition("::")
        group = dict(FILES).get(file_name)
        found = SITE_PARAM.search(name)
        if group in GROUP_SITES:
            needed |= set(GROUP_SITES[group])
        elif group in (FIRST, LAST) and found is not None and found[1] in SITES:
            needed.add(found[1])
        else:
            return set(SITES)
    return needed


def retry_closure(targets: Sequence[str], every: Sequence[str]) -> list[str]:
    """infra 재시도 대상 묶기 — 재시도는 새 세션(새 VM, c0 새로)이라 외부 접속 0의 뜻이 바뀐다.
    처음 대상(`targets`) 기준으로 한 번만 더한다(연쇄하지 않는다): 한 현장의 시나리오 시험이
    대상이면 그 현장의 맨 끝 시험(외부 접속 0)을 더하고(다시 돈 시나리오의 외부 접속을 본다), 맨 끝
    시험이 대상이면 그 현장의 시나리오 시험 전부를 더한다(시나리오 없는 세션의 카운터로 통과하지
    않게). 현장은 `FILES` 표에서 읽는다 — 시험을 더하면 묶음이 따라온다. `every`(첫 실행의 시험 id
    전부)에서 고르고 그 순서로 돌려준다."""
    found = [f for t in targets if (f := _sites_of(t)) is not None]
    want = {
        ("last" if kind == "scenario" else "scenario", s) for kind, sites in found for s in sites
    }

    def wanted(test_id: str) -> bool:
        own = _sites_of(test_id)
        return own is not None and any((own[0], s) in want for s in own[1])

    chosen = set(targets) | {t for t in every if wanted(t)}
    return [t for t in every if t in chosen] + [t for t in targets if t not in every]


def in_central(file_name: str | None) -> bool:
    """시험 파일이 중앙이 필요한 묶음(`CENTRAL_GROUPS`)인가 — 파일이 없으면(세션 끝) 거짓."""
    return file_name is not None and dict(FILES).get(file_name) in CENTRAL_GROUPS


def needs_central(test_ids: Sequence[str]) -> bool:
    """선택된 시험 중 중앙(c1 쌍)이 필요한 묶음(`CENTRAL_GROUPS`)의 것이 있는가 — 없으면 fleet
    세션을 만들지 않는다(c0 시나리오만 고른 재시도가 중앙을 띄우지 않게)."""
    return any(in_central(t.partition("::")[0]) for t in test_ids)
