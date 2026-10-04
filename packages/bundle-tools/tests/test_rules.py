"""순수 판정 규칙: generation·스키마 envelope·Updater 버전·드래프트 게이트."""

from __future__ import annotations

import pytest

from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.manifest import SchemaEnvelope
from pureiron_bundle.verify import (
    AttemptResult,
    DraftState,
    GenDecision,
    GenState,
    SchemaAction,
    check_draft_gate,
    check_updater_version,
    decide_generation,
    decide_schema,
    parse_semver,
)

P, S, F, R = (
    AttemptResult.PENDING,
    AttemptResult.SUCCEEDED,
    AttemptResult.FAILED,
    AttemptResult.REJECTED,
)


@pytest.mark.parametrize(
    ("gen", "state", "expected"),
    [
        (1, GenState(), GenDecision.APPLY),
        (6, GenState(5, 5, S), GenDecision.APPLY),
        (5, GenState(5, 5, S), GenDecision.NO_OP),
        (6, GenState(5, 6, P), GenDecision.RESUME),
        (7, GenState(5, 6, F), GenDecision.APPLY),
        (6, GenState(None, 6, P), GenDecision.RESUME),
        (4, GenState(5, 5, S), RejectCode.E_GEN),
        (5, GenState(5, 6, F), RejectCode.E_GEN),
        (6, GenState(5, 6, F), RejectCode.E_GEN),
        (6, GenState(5, 6, R), RejectCode.E_GEN),
        (6, GenState(5, 6, S), RejectCode.E_GEN),
    ],
)
def test_generation_rule(gen: int, state: GenState, expected: GenDecision | RejectCode) -> None:
    if isinstance(expected, GenDecision):
        assert decide_generation(gen, state) is expected
    else:
        with pytest.raises(BundleRejected) as info:
            decide_generation(gen, state)
        assert info.value.code is expected


E = 5
ENVELOPE = SchemaEnvelope(target=E, app_expected=E, app_max_compatible=E + 1, compat_app_min="x")


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (E - 2, RejectCode.E_SCHEMA),
        (E - 1, SchemaAction.MIGRATE),
        (E, SchemaAction.NO_MIGRATE),
        (E + 1, SchemaAction.NO_MIGRATE),
        (E + 2, RejectCode.E_SCHEMA),
    ],
)
def test_schema_envelope_five_branches(current: int, expected: SchemaAction | RejectCode) -> None:
    if isinstance(expected, SchemaAction):
        decision = decide_schema(ENVELOPE, current)
        assert decision.action is expected
        assert decision.target == (E if expected is SchemaAction.MIGRATE else None)
    else:
        with pytest.raises(BundleRejected) as info:
            decide_schema(ENVELOPE, current)
        assert info.value.code is expected


def test_fresh_install_migrates_from_zero() -> None:
    decision = decide_schema(ENVELOPE, None)
    assert (decision.action, decision.current, decision.target) == (SchemaAction.MIGRATE, 0, E)


@pytest.mark.parametrize(
    "envelope",
    [
        SchemaEnvelope(target=E, app_expected=E, app_max_compatible=E + 2, compat_app_min="x"),
        SchemaEnvelope(target=E, app_expected=E, app_max_compatible=E, compat_app_min="x"),
        SchemaEnvelope(target=E + 1, app_expected=E, app_max_compatible=E + 1, compat_app_min="x"),
    ],
)
def test_inconsistent_envelope_is_e_schema(envelope: SchemaEnvelope) -> None:
    with pytest.raises(BundleRejected) as info:
        decide_schema(envelope, E)
    assert info.value.code is RejectCode.E_SCHEMA


def test_updater_version_is_semver_not_string_order() -> None:
    assert parse_semver("0.10.0") > parse_semver("0.9.0")
    check_updater_version("0.9.0", "0.10.0")
    check_updater_version("1.0.0", "1.0.0")
    with pytest.raises(BundleRejected) as info:
        check_updater_version("0.10.0", "0.9.0")
    assert info.value.code is RejectCode.E_UPDATER
    with pytest.raises(ValueError, match="MAJOR"):
        parse_semver("1.0")


@pytest.mark.parametrize(
    ("absorbs", "draft", "ok"),
    [
        ({"site-a": 3}, DraftState(site_rev=3, base_site_rev=1), True),
        ({"site-a": 3}, DraftState(site_rev=4, base_site_rev=4), False),
        ({}, DraftState(site_rev=2, base_site_rev=2), True),
        ({"site-b": 9}, DraftState(site_rev=3, base_site_rev=2), False),
        ({}, None, True),
    ],
)
def test_draft_gate(absorbs: dict[str, int], draft: DraftState | None, ok: bool) -> None:
    if ok:
        check_draft_gate("site-a", absorbs, draft)
    else:
        with pytest.raises(BundleRejected) as info:
            check_draft_gate("site-a", absorbs, draft)
        assert info.value.code is RejectCode.E_DRAFT
