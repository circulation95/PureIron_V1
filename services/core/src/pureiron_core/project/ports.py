"""다른 모듈이 project에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
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


# --- 장비·태그 정의(M3a, `devices.definition`·`tags.definition`) ----------------------------


class DefinitionsUnavailableError(Exception):
    """site-db·스키마 문제로 정의를 읽지 못했다(호출자는 기다렸다 다시 시도한다)."""


@dataclass(frozen=True, slots=True)
class DeviceDef:
    """`devices/<id>.yaml` 정규형(스키마 기본값 생략). 기본값은 소비자가 채운다."""

    device_id: str
    definition: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TagDef:
    tag_id: int
    path: str
    device_id: str
    definition: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Definitions:
    """한 시점의 활성(retired 아님) 정의. `rev`가 AcquisitionPlan.plan_rev다."""

    rev: int
    devices: tuple[DeviceDef, ...] = ()
    tags: tuple[TagDef, ...] = ()
    # project.yaml `historian` 정규형(스키마 기본값 생략, 없으면 빈 값). 기본값은 소비자가 채운다.
    historian: Mapping[str, object] = field(default_factory=dict[str, object])


class DefinitionsSource(Protocol):
    async def rev(self) -> int:
        """`project_state.definitions_rev`(가벼운 변경 감지용)."""
        ...

    async def load(self) -> Definitions: ...
