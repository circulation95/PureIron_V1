"""`pureiron-project gate` — 번들을 만들기 전 드래프트 게이트 사전 점검(M5-3a K6).

현장 드래프트 리비전(`site_rev`·`base_site_rev`)을 서명된 `draft.tar`(+ enrollment 파일, absorb와
같은 검증·`project_id` 일치) 또는 숫자 인자로 받아 repo `project.yaml absorbs`와 맞춰 본다. 판정은
번들 verify ⑥·Updater `locked` 재검사와 같은 `pureiron_bundle.verify.check_draft_gate`다(M4
publish도 같은 함수).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.verify import DraftState, check_draft_gate
from pureiron_project.absorb import (
    AbsorbError,
    SiteDraft,
    absorbs_of,
    check_project_id,
    open_site_draft,
)
from pureiron_project.project import load_project


class GateError(Exception):
    """게이트 거부(`E_DRAFT`)·드래프트 `project_id` 불일치·점검할 수 없는 입력 — 모두 exit 1."""


@dataclass(frozen=True, slots=True)
class GateResult:
    site_id: str
    state: DraftState
    absorbed: int | None
    fingerprint: str | None
    """`--draft`로 점검했으면 검증에 쓴 현장 키 fingerprint."""

    @property
    def line(self) -> str:
        absorbed = "(none)" if self.absorbed is None else str(self.absorbed)
        return (
            f"ok: site {self.site_id} site_rev={self.state.site_rev} "
            f"base_site_rev={self.state.base_site_rev} absorbs={absorbed}"
        )


def open_gate_draft(draft: bytes, enrollment: bytes, site_id: str) -> SiteDraft:
    """서명된 드래프트 검증 — absorb와 같다(트리·hash 포함)."""
    try:
        return open_site_draft(draft, enrollment, site_id)
    except AbsorbError as exc:
        raise GateError(str(exc)) from exc


def gate(root: Path, site_id: str, source: DraftState | SiteDraft) -> GateResult:
    """`source`가 서명된 드래프트면 그 `project_id`가 repo와 같아야 한다."""
    project = load_project(root)
    if not project.ok:
        raise GateError("project is invalid: " + "; ".join(project.problems))
    try:
        absorbs = absorbs_of(project)
        if isinstance(source, SiteDraft):
            check_project_id(project, source)
    except AbsorbError as exc:
        raise GateError(str(exc)) from exc
    if isinstance(source, SiteDraft):
        state = DraftState(site_rev=source.site_rev, base_site_rev=source.base_site_rev)
        fingerprint = source.fingerprint
    else:
        state, fingerprint = source, None
    try:
        check_draft_gate(site_id, absorbs, state)
    except BundleRejectedError as exc:
        raise GateError(str(exc)) from exc  # "E_DRAFT: …"
    return GateResult(site_id, state, absorbs.get(site_id), fingerprint)
