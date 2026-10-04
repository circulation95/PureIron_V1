"""`recover()` — systemd 시작 시(그리고 `pureiron-site recover`) 끊긴 시도를 수렴시킨다(plan M2).

원칙: 서비스 영향 이전 단계는 창 밖이면 대기로 되돌리고, `migrating` 이후는 창과 무관하게 끝까지
(성공 또는 롤백) 간다. 반쯤 전환된 상태로 남지 않는다.

| 저장된 phase | 복구 |
|---|---|
| `fetched` | 번들이 있으면 검증부터, 없으면 시도를 버린다 |
| `verified` | 재검증(bundle-tools, 추출 다시) 후 창 대기 |
| `committed`·`locked`·`backed_up` | 잠금 해제 → 창 열림 `committed`, 닫힘 `verified`(pending) |
| `migrating` | 새 이미지로 `migrate up` 재실행(멱등) 후 계속 |
| `switching` | target `up -d` 재실행 후 `checking` |
| `checking` | `switching`으로 되돌려 target `up -d`를 명시적으로 다시 한 뒤 `checking` |
| `rolling_back` | 직전 compose `up -d` + 프로젝트 restore 재실행 |
| 종료 phase | 잠금 해제 → 보고 → 확정 |
"""

from __future__ import annotations

import logging

from pureiron_updater.phases import Updater
from pureiron_updater.state import Phase, UpdaterState

log = logging.getLogger(__name__)

_PRE_SERVICE = frozenset({Phase.COMMITTED, Phase.LOCKED, Phase.BACKED_UP})


def recover(updater: Updater) -> UpdaterState:
    attempt = updater.state.attempt
    if attempt is None:
        return updater.state
    log.info("recovering generation %s from %s", attempt.generation, attempt.phase.value)
    if attempt.phase is Phase.VERIFIED:
        # 재검증: 추출본·이미지·compose 정의를 같은 코드로 다시 확인한다.
        updater.reset_phase(attempt, Phase.FETCHED)
    elif attempt.phase in _PRE_SERVICE:
        if not attempt.first_install:
            attempt = updater.back_to_verified(attempt)
        if updater.window_is_open(attempt):
            updater.reset_phase(attempt, Phase.COMMITTED)
        else:
            updater.reset_phase(attempt, Phase.VERIFIED)
    elif attempt.phase is Phase.CHECKING:
        updater.reset_phase(attempt, Phase.SWITCHING)
    return updater.advance()
