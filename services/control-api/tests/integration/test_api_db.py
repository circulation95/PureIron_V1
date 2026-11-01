"""control-api 종료 기준(foundation M4, plan M4-0)을 실제 PostgreSQL 16 위에서 확인한다.

gen 역행 PUT 409 · 다른 현장 token 403 · 서명 불일치 400 · seq ≤ last_seq 반입은 이력만
(current 미갱신) · last_seen 180s 초과 stale · 폐쇄망 반입 source=offline · desired ETag 304 ·
번들 Range 206.
enrollment·폐쇄망 보고 파일은 실제 Updater 코드(`pureiron_updater.enroll`)가 만든다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from pureiron_bundle.sitesign import SitePurpose

from .conftest import Central
from .site_kit import Site, build_pib

pytestmark = pytest.mark.integration

type JsonObject = dict[str, object]


def register(central: Central, site: Site) -> str:
    response = central.client.post(
        "/api/v1/sites", content=site.enroll(), headers=central.as_role("admin")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["site_id"] == site.site_id
    assert body["next_seq"] == 1
    assert body["token"].startswith("pis_")
    return body["token"]


def publish(
    central: Central,
    tmp_path: Path,
    site_id: str,
    gen: int,
    *,
    project_version: str = "v3",
    padding_bytes: int = 0,
) -> bytes:
    out = tmp_path / f"{site_id}-g{gen}.pib"
    pib = build_pib(out, site_id, gen, project_version=project_version, padding_bytes=padding_bytes)
    data = pib.read_bytes()
    response = central.client.put(
        f"/api/v1/bundles/{site_id}/{gen}", content=data, headers=central.as_role("ci")
    )
    assert response.status_code == 201, response.text
    return data


def fleet_row(central: Central, site_id: str) -> JsonObject:
    response = central.client.get(
        f"/api/v1/fleet?site={site_id}", headers=central.as_role("viewer")
    )
    assert response.status_code == 200, response.text
    rows = cast(list[JsonObject], response.json())
    assert [row["site"] for row in rows] == [site_id]
    return rows[0]


@pytest.fixture
def site_a(tmp_path: Path) -> Site:
    return Site(tmp_path, "site-a")


@pytest.fixture
def site_b(tmp_path: Path) -> Site:
    return Site(tmp_path, "site-b")


def test_registration_is_admin_only_and_returns_the_token_once(
    central: Central, site_a: Site
) -> None:
    enrollment = site_a.enroll()
    assert central.client.post("/api/v1/sites", content=enrollment).status_code == 401
    for role in ("viewer", "ci"):
        denied = central.client.post(
            "/api/v1/sites", content=enrollment, headers=central.as_role(role)
        )
        assert denied.status_code == 403
    token = register(central, site_a)
    again = central.client.post(
        "/api/v1/sites", content=enrollment, headers=central.as_role("admin")
    )
    assert again.status_code == 409
    # 자기 서명이 깨진 enrollment는 400.
    forged = enrollment.replace(b'"site-a"', b'"site-c"')
    bad = central.client.post("/api/v1/sites", content=forged, headers=central.as_role("admin"))
    assert bad.status_code == 400
    # token 원문은 DB에 없다(조회는 SHA-256으로만 된다).
    assert (
        central.client.get("/api/v1/sites/site-a/desired", headers=central.auth(token)).status_code
        == 404
    )


def test_other_site_token_is_403_on_every_site_path(
    central: Central, site_a: Site, site_b: Site
) -> None:
    token_a = register(central, site_a)
    register(central, site_b)
    headers = central.auth(token_a)
    other = "/api/v1/sites/site-b"
    attempts = [
        central.client.post(
            f"{other}/heartbeat", content=site_a.heartbeat(1, gen=None), headers=headers
        ),
        central.client.get(f"{other}/desired", headers=headers),
        central.client.get(f"{other}/bundles/1", headers=headers),
        central.client.post(f"{other}/reports", content=site_a.report(1, to=1), headers=headers),
        central.client.post(
            f"{other}/drafts", content=site_a.draft(site_rev=1, base_site_rev=0), headers=headers
        ),
    ]
    assert [r.status_code for r in attempts] == [403] * 5
    # 현장 token으로 사람 경로도 403.
    assert central.client.get("/api/v1/fleet", headers=headers).status_code == 403
    assert (
        central.client.put("/api/v1/bundles/site-a/1", content=b"x", headers=headers).status_code
        == 403
    )


def test_signature_mismatch_report_is_400_and_foreign_payload_is_403(
    central: Central, site_a: Site, site_b: Site
) -> None:
    token_a = register(central, site_a)
    register(central, site_b)
    headers = central.auth(token_a)
    # site-b 키로 서명했지만 payload는 site-a라고 적은 보고 → 등록 키와 서명이 다르다.
    forged = site_b.signed(
        SitePurpose.REPORT, {"site_id": "site-a", "report_seq": 1, "result": "x"}
    )
    assert (
        central.client.post(
            "/api/v1/sites/site-a/reports", content=forged, headers=headers
        ).status_code
        == 400
    )
    # 서명 뒤 payload를 바꾼 heartbeat.
    tampered = site_a.heartbeat(1, gen=3).replace(b'"gen": 3', b'"gen": 4')
    assert (
        central.client.post(
            "/api/v1/sites/site-a/heartbeat", content=tampered, headers=headers
        ).status_code
        == 400
    )
    # 용도가 다른 서명(heartbeat 서명을 보고로).
    assert (
        central.client.post(
            "/api/v1/sites/site-a/reports", content=site_a.heartbeat(1, gen=3), headers=headers
        ).status_code
        == 400
    )
    # 자기 키로 서명했어도 payload가 다른 현장이면 403.
    foreign = site_a.signed(SitePurpose.HEARTBEAT, {"site_id": "site-b", "report_seq": 1})
    assert (
        central.client.post(
            "/api/v1/sites/site-a/heartbeat", content=foreign, headers=headers
        ).status_code
        == 403
    )
    # 어느 것도 반영되지 않았다.
    assert fleet_row(central, "site-a")["last_seq"] == 0


def test_generation_regression_is_409(central: Central, site_a: Site, tmp_path: Path) -> None:
    register(central, site_a)
    publish(central, tmp_path, "site-a", 2)
    ci = central.as_role("ci")
    for gen in (2, 1):
        pib = build_pib(tmp_path / f"again-{gen}.pib", "site-a", gen).read_bytes()
        response = central.client.put(f"/api/v1/bundles/site-a/{gen}", content=pib, headers=ci)
        assert response.status_code == 409, response.text
    publish(central, tmp_path, "site-a", 3)
    # manifest site·gen이 경로와 다르면 400, `.pib`가 아니면 400, 미등록 현장 404, admin 게시 403.
    wrong = build_pib(tmp_path / "wrong.pib", "site-a", 5).read_bytes()
    assert (
        central.client.put("/api/v1/bundles/site-a/4", content=wrong, headers=ci).status_code == 400
    )
    assert (
        central.client.put("/api/v1/bundles/site-a/4", content=b"junk", headers=ci).status_code
        == 400
    )
    assert (
        central.client.put("/api/v1/bundles/site-x/1", content=wrong, headers=ci).status_code == 404
    )
    assert (
        central.client.put(
            "/api/v1/bundles/site-a/4", content=wrong, headers=central.as_role("admin")
        ).status_code
        == 403
    )
    row = fleet_row(central, "site-a")
    assert row["max_published_gen"] == 3
    desired = cast(JsonObject, row["desired"])
    assert (desired["product_version"], desired["project_version"]) == ("2026.11.0", "v3")
    # 거부된 업로드는 디스크에 남지 않는다(게시된 g2·g3만).
    files = sorted(p.name for p in (central.settings.data_dir / "bundles" / "site-a").iterdir())
    assert files == ["g2.pib", "g3.pib"]


def test_desired_etag_304_and_bundle_range_206(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    headers = central.auth(register(central, site_a))
    assert central.client.get("/api/v1/sites/site-a/desired", headers=headers).status_code == 404
    data = publish(central, tmp_path, "site-a", 1, padding_bytes=40_000)
    response = central.client.get("/api/v1/sites/site-a/desired", headers=headers)
    assert response.status_code == 200
    assert response.headers["etag"] == '"1"'
    assert response.json() == {
        "generation": 1,
        "bundle_url": "/api/v1/sites/site-a/bundles/1",
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    cached = central.client.get(
        "/api/v1/sites/site-a/desired", headers={**headers, "If-None-Match": '"1"'}
    )
    assert cached.status_code == 304
    assert cached.content == b""
    full = central.client.get("/api/v1/sites/site-a/bundles/1", headers=headers)
    assert (full.status_code, full.content) == (200, data)
    part = central.client.get(
        "/api/v1/sites/site-a/bundles/1", headers={**headers, "Range": "bytes=10000-"}
    )
    assert part.status_code == 206
    assert part.content == data[10000:]
    assert part.headers["content-range"] == f"bytes 10000-{len(data) - 1}/{len(data)}"
    assert central.client.get("/api/v1/sites/site-a/bundles/2", headers=headers).status_code == 404
    # 새 게시는 ETag를 바꾼다.
    publish(central, tmp_path, "site-a", 2)
    newer = central.client.get(
        "/api/v1/sites/site-a/desired", headers={**headers, "If-None-Match": '"1"'}
    )
    assert (newer.status_code, newer.headers["etag"]) == (200, '"2"')


def test_offline_import_at_or_below_last_seq_is_history_only(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    headers = central.auth(register(central, site_a))
    publish(central, tmp_path, "site-a", 2)
    admin = central.as_role("admin")
    # 폐쇄망 보고 2건(seq 1·2) — 실제 `report export` 파일.
    site_a.queue_report(to=1)
    site_a.queue_report(to=2)
    first = site_a.export(tmp_path / "usb1")
    imported = central.client.post("/api/v1/offline-imports", content=first, headers=admin)
    assert imported.status_code == 201, imported.text
    assert {k: imported.json()[k] for k in ("applied_total", "history_total", "last_seq")} == {
        "applied_total": 2,
        "history_total": 0,
        "last_seq": 2,
    }
    row = fleet_row(central, "site-a")
    assert row["connection"] == "never"  # 폐쇄망 반입은 last_seen을 바꾸지 않는다
    current = cast(JsonObject, row["current"])
    assert (current["gen"], current["source"], current["project_version"]) == (2, "offline", "v3")
    attempt = cast(JsonObject, row["last_attempt"])
    assert (attempt["report_seq"], attempt["source"], attempt["to"], attempt["from"]) == (
        2,
        "offline",
        2,
        1,
    )
    # 온라인 heartbeat seq 5 → 반영(current gen 7, online).
    beat = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(5, gen=7), headers=headers
    )
    assert beat.json() == {"outcome": "applied", "last_seq": 5}
    # 늦게 반출된 보고(seq 3 ≤ last_seq 5) → 이력만, current·last_attempt 그대로.
    site_a.queue_report(to=3, result="failed")
    late = central.client.post(
        "/api/v1/offline-imports", content=site_a.export(tmp_path / "usb2"), headers=admin
    )
    assert late.status_code == 201
    assert (late.json()["applied_total"], late.json()["history_total"]) == (0, 1)
    row = fleet_row(central, "site-a")
    current = cast(JsonObject, row["current"])
    assert (current["gen"], current["source"]) == (7, "online")
    assert cast(JsonObject, row["last_attempt"])["report_seq"] == 2
    assert row["last_seq"] == 5
    # 같은 파일을 다시 반입해도 이력만 늘고 상태는 그대로다.
    again = central.client.post("/api/v1/offline-imports", content=first, headers=admin)
    assert (again.json()["applied_total"], again.json()["history_total"]) == (0, 2)
    # 같은 seq 온라인 재전송도 200 + history.
    retry = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(5, gen=8), headers=headers
    )
    assert (retry.status_code, retry.json()["outcome"]) == (200, "history")
    assert cast(JsonObject, fleet_row(central, "site-a")["current"])["gen"] == 7


def test_offline_import_rejects_bad_files(
    central: Central, site_a: Site, site_b: Site, tmp_path: Path
) -> None:
    register(central, site_a)
    admin = central.as_role("admin")
    site_a.queue_report(to=1)
    exported = site_a.export(tmp_path / "usb")
    tampered = exported.replace(b'"to": 1', b'"to": 9')
    assert (
        central.client.post("/api/v1/offline-imports", content=tampered, headers=admin).status_code
        == 400
    )
    # 미등록 현장의 파일은 404, viewer는 반입 못 함.
    site_b.enroll()
    site_b.queue_report(to=1)
    unknown = site_b.export(tmp_path / "usb-b")
    assert (
        central.client.post("/api/v1/offline-imports", content=unknown, headers=admin).status_code
        == 404
    )
    assert (
        central.client.post(
            "/api/v1/offline-imports", content=exported, headers=central.as_role("viewer")
        ).status_code
        == 403
    )


def test_connection_goes_stale_after_three_polls(
    central: Central, site_a: Site, site_b: Site
) -> None:
    headers = central.auth(register(central, site_a))
    register(central, site_b)
    assert (
        central.client.post(
            "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(1, gen=4), headers=headers
        ).status_code
        == 200
    )
    seen = fleet_row(central, "site-a")
    assert seen["connection"] == "online"
    central.clock.advance(180)
    assert fleet_row(central, "site-a")["connection"] == "online"
    central.clock.advance(1)
    stale = fleet_row(central, "site-a")
    assert stale["connection"] == "stale"
    # stale이어도 마지막 보고값과 그 시각은 그대로 낸다.
    assert stale["current"] == seen["current"]
    assert stale["last_seen"] == seen["last_seen"]
    assert fleet_row(central, "site-b")["connection"] == "never"
    everyone = central.client.get("/api/v1/fleet", headers=central.as_role("admin")).json()
    assert [row["site"] for row in everyone] == ["site-a", "site-b"]
    # 밀린 seq의 재전송도 연결 증거다: online으로 돌아오지만 current는 그대로(이력만).
    resent = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(1, gen=9), headers=headers
    )
    assert resent.json() == {"outcome": "history", "last_seq": 1}
    back = fleet_row(central, "site-a")
    assert back["connection"] == "online"
    assert back["current"] == seen["current"]


def test_online_report_updates_last_attempt_and_current_on_success(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    headers = central.auth(register(central, site_a))
    publish(central, tmp_path, "site-a", 1, project_version="v9")
    failed = central.client.post(
        "/api/v1/sites/site-a/reports",
        content=site_a.report(1, to=1, result="failed"),
        headers=headers,
    )
    assert failed.json() == {"outcome": "applied", "last_seq": 1}
    row = fleet_row(central, "site-a")
    assert row["current"] is None
    assert cast(JsonObject, row["last_attempt"])["result"] == "failed"
    ok = central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(2, to=1), headers=headers
    )
    assert ok.json()["outcome"] == "applied"
    row = fleet_row(central, "site-a")
    current = cast(JsonObject, row["current"])
    assert (current["gen"], current["project_version"], current["source"]) == (1, "v9", "online")
    assert row["connection"] == "online"


def test_rekey_revokes_the_old_key_and_returns_next_seq(central: Central, site_a: Site) -> None:
    headers = central.auth(register(central, site_a))
    old_report = site_a.report(4, to=1)
    assert (
        central.client.post(
            "/api/v1/sites/site-a/reports", content=site_a.report(3, to=1), headers=headers
        ).json()["last_seq"]
        == 3
    )
    new_enrollment = site_a.enroll(re_enroll=True)
    admin = central.as_role("admin")
    assert (
        central.client.post(
            "/api/v1/sites/site-b/rekey", content=new_enrollment, headers=admin
        ).status_code
        == 403
    )
    rekeyed = central.client.post(
        "/api/v1/sites/site-a/rekey", content=new_enrollment, headers=admin
    )
    assert rekeyed.status_code == 200, rekeyed.text
    assert rekeyed.json()["next_seq"] == 4
    # 구 키 서명은 이제 400, 새 키 서명은 반영.
    assert (
        central.client.post(
            "/api/v1/sites/site-a/reports", content=old_report, headers=headers
        ).status_code
        == 400
    )
    assert central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(4, to=1), headers=headers
    ).json() == {"outcome": "applied", "last_seq": 4}
    assert (
        central.client.post(
            "/api/v1/sites/site-x/rekey", content=new_enrollment, headers=admin
        ).status_code
        == 403
    )


def test_site_token_reissue_and_api_token_revoke(central: Central, site_a: Site) -> None:
    old = register(central, site_a)
    admin = central.as_role("admin")
    reissued = central.client.post("/api/v1/sites/site-a/token", headers=admin)
    assert reissued.status_code == 200
    new = reissued.json()["token"]
    assert new != old
    desired = "/api/v1/sites/site-a/desired"
    assert central.client.get(desired, headers=central.auth(old)).status_code == 401
    assert central.client.get(desired, headers=central.auth(new)).status_code == 404
    assert central.client.post("/api/v1/sites/site-x/token", headers=admin).status_code == 404
    central.cli("token", "revoke", "--name", "it-admin")
    assert central.client.get("/api/v1/fleet", headers=admin).status_code == 401
    listed = cast(list[JsonObject], central.cli("token", "list")["tokens"])
    assert [(t["name"], t["revoked_at"] is not None) for t in listed] == [("it-admin", True)]


def test_drafts_upload_and_vendor_download(central: Central, site_a: Site, site_b: Site) -> None:
    headers = central.auth(register(central, site_a))
    register(central, site_b)
    draft = site_a.draft(site_rev=7, base_site_rev=5)
    uploaded = central.client.post("/api/v1/sites/site-a/drafts", content=draft, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    assert (uploaded.json()["site_rev"], uploaded.json()["base_site_rev"]) == (7, 5)
    latest = central.client.get(
        "/api/v1/sites/site-a/drafts/latest", headers=central.as_role("admin")
    )
    assert latest.status_code == 200
    assert latest.content == draft
    assert latest.headers["x-pureiron-draft-site-rev"] == "7"
    # site-b 키로 서명한 site-a 드래프트(서명 불일치) 400, viewer 내려받기 403, 없으면 404.
    forged = site_b.draft(site_rev=8, base_site_rev=5).replace(b"site-b", b"site-a")
    assert (
        central.client.post(
            "/api/v1/sites/site-a/drafts", content=forged, headers=headers
        ).status_code
        == 400
    )
    assert (
        central.client.get(
            "/api/v1/sites/site-a/drafts/latest", headers=central.as_role("viewer")
        ).status_code
        == 403
    )
    assert (
        central.client.get(
            "/api/v1/sites/site-b/drafts/latest", headers=central.as_role("admin")
        ).status_code
        == 404
    )


def test_fleet_roles_and_unknown_site(central: Central, site_a: Site) -> None:
    register(central, site_a)
    assert central.client.get("/api/v1/fleet", headers=central.as_role("ci")).status_code == 403
    assert (
        central.client.get(
            "/api/v1/fleet?site=site-x", headers=central.as_role("viewer")
        ).status_code
        == 404
    )


def test_migrate_cli_and_readyz(central: Central) -> None:
    status = central.cli("migrate", "status")
    assert (status["cur"], status["app_expected"], status["pending"], status["checksum_ok"]) == (
        1,
        1,
        [],
        True,
    )
    code, out = central.run("migrate", "up")
    assert (code, out.strip()) == (0, "migrate up: applied 0")
    assert central.run("migrate", "verify") == (0, "migrate verify: ok\n")
    ready = central.client.get("/api/v1/readyz")
    assert ready.status_code == 200
    assert ready.json()["schema_envelope"]["cur"] == 1
