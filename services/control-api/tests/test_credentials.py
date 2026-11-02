"""인증 표(plan M4 C5): 현장 token은 자기 `{site}`만, 사람·CI token은 역할로 — 그 밖은 401/403."""

from __future__ import annotations

import pytest

from pureiron_control.credentials import (
    AnyRole,
    ApiPrincipal,
    Decision,
    OwnSite,
    Principal,
    Requirement,
    Role,
    SitePrincipal,
    authorize,
)
from pureiron_control.platform.tokens import TokenKind, new_token, token_kind, token_sha256

SITE_A = SitePrincipal("site-a")
ADMIN = ApiPrincipal(1, "ops", Role.ADMIN)
CI = ApiPrincipal(2, "publish", Role.CI)
VIEWER = ApiPrincipal(3, "board", Role.VIEWER)

ADMIN_ONLY = AnyRole(frozenset({Role.ADMIN}))
CI_ONLY = AnyRole(frozenset({Role.CI}))
FLEET_READERS = AnyRole(frozenset({Role.ADMIN, Role.VIEWER}))

ALLOW, DENY, ANON = Decision.ALLOW, Decision.FORBIDDEN, Decision.UNAUTHENTICATED


@pytest.mark.parametrize(
    ("principal", "requirement", "path_site", "expected"),
    [
        # 현장 경로(heartbeat·desired·bundles·reports·drafts)
        (SITE_A, OwnSite(), "site-a", ALLOW),
        (SITE_A, OwnSite(), "site-b", DENY),
        (ADMIN, OwnSite(), "site-a", DENY),
        (CI, OwnSite(), "site-a", DENY),
        (None, OwnSite(), "site-a", ANON),
        # admin 경로(POST /sites·rekey·token·offline-imports·drafts/latest)
        (ADMIN, ADMIN_ONLY, None, ALLOW),
        (CI, ADMIN_ONLY, None, DENY),
        (VIEWER, ADMIN_ONLY, None, DENY),
        (SITE_A, ADMIN_ONLY, "site-a", DENY),
        (None, ADMIN_ONLY, None, ANON),
        # 게시(PUT /bundles) = ci만
        (CI, CI_ONLY, "site-a", ALLOW),
        (ADMIN, CI_ONLY, "site-a", DENY),
        (SITE_A, CI_ONLY, "site-a", DENY),
        # GET /fleet = admin·viewer
        (ADMIN, FLEET_READERS, None, ALLOW),
        (VIEWER, FLEET_READERS, None, ALLOW),
        (CI, FLEET_READERS, None, DENY),
        (SITE_A, FLEET_READERS, None, DENY),
    ],
)
def test_authorization_table(
    principal: Principal | None, requirement: Requirement, path_site: str | None, expected: Decision
) -> None:
    assert authorize(principal, requirement, path_site=path_site) is expected


def test_tokens_carry_their_kind_and_hash_is_sha256_hex() -> None:
    site, api = new_token(TokenKind.SITE), new_token(TokenKind.API)
    assert token_kind(site) is TokenKind.SITE
    assert token_kind(api) is TokenKind.API
    assert site != new_token(TokenKind.SITE)
    # 기대값: `printf 'pis_abc' | sha256sum`
    assert token_sha256("pis_abc") == (
        "a12e63da037bd4309e2661262c7478b056056348ea4b0c2daec372da24de8559"
    )


@pytest.mark.parametrize("token", ["", "pis_", "pia_", "Bearer x", "abc", "PIS_abc"])
def test_unknown_token_shapes_have_no_kind(token: str) -> None:
    assert token_kind(token) is None
