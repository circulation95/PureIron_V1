"""`pureiron-project gate` — 번들을 만들기 전 드래프트 게이트 사전 점검(M5-3a K6).

현장 드래프트 리비전(`site_rev`·`base_site_rev`)을 서명된 `draft.tar`(+ enrollment 파일, absorb와
같은 검증) 또는 숫자 인자로 받아 repo `project.yaml absorbs`와 맞춰 본다. 판정은 번들 verify ⑥·
Updater `locked` 재검사와 같은 `pureiron_bundle.verify.check_draft_gate`다(M4 publish도 같은 함수).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.verify import DraftState, check_draft_gate
from pureiron_project.absorb import AbsorbError, absorbs_of, open_site_draft
from pureiron_project.project import load_project


class GateError(Exception):
    """게이트 거부(`E_DRAFT`) 또는 점검할 수 없는 입력 — 둘 다 exit 1."""


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


def draft_state(draft: bytes, enrollment: bytes, site_id: str) -> tuple[DraftState, str]:
    """서명된 드래프트 → (리비전, 현장 키 fingerprint). 검증은 absorb와 같다(트리·hash 포함)."""
    try:
        opened = open_site_draft(draft, enrollment, site_id)
    except AbsorbError as exc:
        raise GateError(str(exc)) from exc
    return DraftState(site_rev=opened.site_rev, base_site_rev=opened.base_site_rev), (
        opened.fingerprint
    )


def gate(
    root: Path, site_id: str, state: DraftState, *, fingerprint: str | None = None
) -> GateResult:
    project = load_project(root)
    if not project.ok:
        raise GateError("project is invalid: " + "; ".join(project.problems))
    try:
        absorbs = absorbs_of(project)
    except AbsorbError as exc:
        raise GateError(str(exc)) from exc
    try:
        check_draft_gate(site_id, absorbs, state)
    except BundleRejectedError as exc:
        raise GateError(str(exc)) from exc  # "E_DRAFT: …"
    return GateResult(site_id, state, absorbs.get(site_id), fingerprint)
