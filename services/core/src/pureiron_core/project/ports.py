"""다른 모듈이 project에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol


class ProjectStateError(Exception):
    pass


class EditLockConflictError(ProjectStateError):
    def __init__(self, holder: str | None) -> None:
        super().__init__(f"edit lock held by {holder!r}")
        self.holder = holder


@dataclass(frozen=True, slots=True)
class ProjectState:
    project_id: str | None
    version: str | None
    content_hash: str | None
    base_site_rev: int
    site_rev: int
    edit_lock_owner: str | None
    edit_lock_gen: int | None
    applied_at: datetime | None

    def to_snapshot(self) -> dict[str, object]:
        data = asdict(self)
        data["applied_at"] = self.applied_at.isoformat() if self.applied_at else None
        return data


class ProjectStatePort(Protocol):
    async def get(self) -> ProjectState: ...

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        """잠금을 잡고 그 시점 site_rev를 돌려준다. 같은 owner·generation 재요청은 멱등이다.

        같은 owner라도 generation이 다르면 충돌(409)이다. Updater는 새 generation으로 다시
        잡기 전에 이전 잠금을 release 해야 한다(M2 recover 경로).
        """
        ...

    async def release_edit_lock(self, owner: str) -> None:
        """owner의 잠금을 푼다. 이미 풀려 있으면 아무것도 하지 않는다."""
        ...
