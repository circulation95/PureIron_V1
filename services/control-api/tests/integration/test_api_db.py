"""control-api 종료 기준(foundation M4, plan M4-0)을 실제 PostgreSQL 16 위에서 확인한다.

gen 역행 PUT 409 · 다른 현장 token 403 · 서명 불일치 400 · seq ≤ 그 종류의 수위 반입은 이력만
(current 미갱신) · last_seen 180s 초과 stale · 폐쇄망 반입 source=offline · desired ETag 304 ·
번들 Range 206. 리뷰 r1: 종류별 수위(늦은 보고가 heartbeat에 가리지 않음)·USB 파일 안
수위 가로지르기·같은 gen 동시 PUT·동시 등록 409. 리뷰 r2: 늦은 heartbeat가 더 새 current를
덮지 않음(current_seq).
enrollment·폐쇄망 보고 파일은 실제 Updater 코드(`pureiron_updater.enroll`)가 만든다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from pureiron_bundle.sitesign import SitePurpose
from pureiron_control.platform.tokens import token_sha256

from .conftest import Central, Request
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
    # token 원문은 DB에 없고 SHA-256만 있다 — 그 해시로 인증된다.
    stored = central.fetch("SELECT token_sha256 FROM site_tokens WHERE site_id = 'site-a'")
    assert [r["token_sha256"] for r in stored] == [token_sha256(token)]
    assert central.fetch("SELECT 1 FROM site_tokens WHERE token_sha256 = $1", token) == []
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
    row = fleet_row(central, "site-a")
    assert (row["last_heartbeat_seq"], row["last_report_seq"]) == (0, 0)


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
    # 온라인 heartbeat seq 5 → 반영(current gen 7, online). 보고 수위(2)는 그대로다.
    beat = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(5, gen=7), headers=headers
    )
    assert beat.json() == {"outcome": "applied", "last_seq": 5}
    # 온라인 보고 seq 6(실패) → 보고 수위 6.
    online = central.client.post(
        "/api/v1/sites/site-a/reports",
        content=site_a.report(6, to=3, result="failed"),
        headers=headers,
    )
    assert online.json() == {"outcome": "applied", "last_seq": 6}
    # 늦게 반출된 보고(seq 3 ≤ 보고 수위 6) → 이력만, current·last_attempt 그대로.
    site_a.queue_report(to=3, result="failed")
    late = central.client.post(
        "/api/v1/offline-imports", content=site_a.export(tmp_path / "usb2"), headers=admin
    )
    assert late.status_code == 201
    assert (late.json()["applied_total"], late.json()["history_total"]) == (0, 1)
    assert late.json()["last_seq"] == 6
    row = fleet_row(central, "site-a")
    current = cast(JsonObject, row["current"])
    assert (current["gen"], current["source"]) == (7, "online")
    assert cast(JsonObject, row["last_attempt"])["report_seq"] == 6
    assert (row["last_heartbeat_seq"], row["last_report_seq"]) == (5, 6)
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
    # 카운터는 공용 — heartbeat 수위 6이 더 크면 다음 seq는 7이다.
    assert (
        central.client.post(
            "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(6, gen=1), headers=headers
        ).json()["last_seq"]
        == 6
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
    assert rekeyed.json()["next_seq"] == 7
    # 구 키 서명은 이제 400, 새 키 서명은 반영.
    assert (
        central.client.post(
            "/api/v1/sites/site-a/reports", content=old_report, headers=headers
        ).status_code
        == 400
    )
    assert central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(7, to=1), headers=headers
    ).json() == {"outcome": "applied", "last_seq": 7}
    assert (
        central.client.post(
            "/api/v1/sites/site-x/rekey", content=new_enrollment, headers=admin
        ).status_code
        == 403
    )


def test_late_report_after_newer_heartbeat_is_still_applied(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    """리뷰 M-1: 보고 seq 11 전송이 끊기고 heartbeat seq 12가 먼저 성공해도, 재전송된 보고 11은
    보고 수위(10)보다 크므로 반영된다 — 실패한 업데이트가 fleet `last_attempt`에 보인다."""
    headers = central.auth(register(central, site_a))
    publish(central, tmp_path, "site-a", 1)
    ok = central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(10, to=1), headers=headers
    )
    assert ok.json() == {"outcome": "applied", "last_seq": 10}
    beat = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(12, gen=1), headers=headers
    )
    assert beat.json() == {"outcome": "applied", "last_seq": 12}
    late = central.client.post(
        "/api/v1/sites/site-a/reports",
        content=site_a.report(11, to=2, result="failed"),
        headers=headers,
    )
    assert late.json() == {"outcome": "applied", "last_seq": 11}
    row = fleet_row(central, "site-a")
    attempt = cast(JsonObject, row["last_attempt"])
    assert (attempt["report_seq"], attempt["result"], attempt["to"]) == (11, "failed", 2)
    assert (row["last_heartbeat_seq"], row["last_report_seq"]) == (12, 11)
    assert cast(JsonObject, row["current"])["gen"] == 1  # 실패 보고는 current를 바꾸지 않는다
    # 같은 보고를 또 보내면(응답 유실) 이번에는 이력만.
    again = central.client.post(
        "/api/v1/sites/site-a/reports",
        content=site_a.report(11, to=2, result="failed"),
        headers=headers,
    )
    assert again.json() == {"outcome": "history", "last_seq": 11}


def test_late_heartbeat_does_not_overwrite_current_of_a_newer_successful_report(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    """리뷰 r2 R-1 재현 A: 수위 hb 10·보고 10에서 hb 11(gen 1)이 늦는 사이 성공 보고 12(to 2)가
    반영된다. 뒤늦게 온 hb 11은 heartbeat 수위로는 반영(phase·disk)되지만, current는 공용 카운터
    순서(current_seq 12)로 걸러져 gen 2·보고 시각 그대로다."""
    headers = central.auth(register(central, site_a))
    publish(central, tmp_path, "site-a", 1)
    publish(central, tmp_path, "site-a", 2, project_version="v4")
    for path, body in (
        ("heartbeat", site_a.heartbeat(10, gen=1)),
        ("reports", site_a.report(10, to=1)),
        ("reports", site_a.report(12, to=2)),
    ):
        sent = central.client.post(f"/api/v1/sites/site-a/{path}", content=body, headers=headers)
        assert sent.json()["outcome"] == "applied", sent.text
    before = cast(JsonObject, fleet_row(central, "site-a")["current"])
    assert (before["gen"], before["project_version"]) == (2, "v4")
    late = central.client.post(
        "/api/v1/sites/site-a/heartbeat",
        content=site_a.heartbeat(11, gen=1, disk_state="low"),
        headers=headers,
    )
    assert late.json() == {"outcome": "applied", "last_seq": 11}
    row = fleet_row(central, "site-a")
    assert cast(JsonObject, row["current"]) == before  # gen·reported_at·source 모두 그대로
    assert row["disk_state"] == "low"  # heartbeat 전용 필드는 heartbeat 수위로 반영
    assert (row["last_heartbeat_seq"], row["last_report_seq"]) == (11, 12)
    # 다음 heartbeat(14)는 공용 카운터로도 더 새로우므로 current를 쓴다.
    next_beat = central.client.post(
        "/api/v1/sites/site-a/heartbeat", content=site_a.heartbeat(14, gen=2), headers=headers
    )
    assert next_beat.json() == {"outcome": "applied", "last_seq": 14}
    after = cast(JsonObject, fleet_row(central, "site-a")["current"])
    assert (after["gen"], after["product"]) == (2, "2026.11.0")
    # 리뷰 r3 N3-b(재현 B): 늦게 재전송된 성공 보고 13(< current_seq 14)은 보고 수위(12)로는
    # 반영(last_attempt)되지만 current는 그대로다.
    resent = central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(13, to=1), headers=headers
    )
    assert resent.json() == {"outcome": "applied", "last_seq": 13}
    row = fleet_row(central, "site-a")
    assert cast(JsonObject, row["last_attempt"])["report_seq"] == 13
    assert cast(JsonObject, row["current"]) == after


def test_offline_file_crossing_the_report_mark_splits_history_and_applied(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    """리뷰 L-8 (c): 보고 수위 5, USB 파일 seq 4..7 → 4·5 이력, 6·7 반영, last_attempt = 7."""
    headers = central.auth(register(central, site_a))
    admin = central.as_role("admin")
    for to in (1, 2, 3):
        site_a.queue_report(to=to)
    first = central.client.post(
        "/api/v1/offline-imports", content=site_a.export(tmp_path / "usb1"), headers=admin
    )
    assert (first.json()["applied_total"], first.json()["last_seq"]) == (3, 3)
    online = central.client.post(
        "/api/v1/sites/site-a/reports", content=site_a.report(5, to=3), headers=headers
    )
    assert online.json() == {"outcome": "applied", "last_seq": 5}
    for to in (4, 5, 6, 7):
        site_a.queue_report(to=to, result="failed")
    crossing = central.client.post(
        "/api/v1/offline-imports", content=site_a.export(tmp_path / "usb2"), headers=admin
    )
    assert crossing.status_code == 201, crossing.text
    body = crossing.json()
    assert (body["applied_total"], body["history_total"], body["last_seq"]) == (2, 2, 7)
    rows = central.fetch(
        "SELECT report_seq, outcome FROM apply_attempts WHERE offline_import_id = $1"
        " ORDER BY report_seq",
        body["import_id"],
    )
    assert [(r["report_seq"], r["outcome"]) for r in rows] == [
        (4, "history"),
        (5, "history"),
        (6, "applied"),
        (7, "applied"),
    ]
    row = fleet_row(central, "site-a")
    attempt = cast(JsonObject, row["last_attempt"])
    assert (attempt["report_seq"], attempt["source"], attempt["to"]) == (7, "offline", 7)
    assert row["last_report_seq"] == 7


def test_concurrent_same_generation_publish_is_one_201_and_one_409(
    central: Central, site_a: Site, tmp_path: Path
) -> None:
    """리뷰 L-8 (a): 같은 gen PUT 두 개가 동시에 오면 현장 행 잠금이 줄을 세워 하나만 게시된다."""
    register(central, site_a)
    ci = central.as_role("ci")
    pibs = [
        build_pib(tmp_path / f"race-{i}.pib", "site-a", 1, project_version=f"v{i}").read_bytes()
        for i in (1, 2)
    ]
    responses = central.concurrently(
        *(Request("PUT", "/api/v1/bundles/site-a/1", pib, ci) for pib in pibs)
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    winner = pibs[[r.status_code for r in responses].index(201)]
    # 디스크에는 이긴 쪽 파일 하나만 남는다(진 쪽 임시 파일도 지워진다).
    site_dir = central.settings.data_dir / "bundles" / "site-a"
    assert sorted(p.name for p in site_dir.iterdir()) == ["g1.pib"]
    assert (site_dir / "g1.pib").read_bytes() == winner
    desired = cast(JsonObject, fleet_row(central, "site-a")["desired"])
    assert desired["sha256"] == "sha256:" + hashlib.sha256(winner).hexdigest()


def test_concurrent_registration_of_the_same_site_is_one_201_and_one_409(
    central: Central, site_a: Site
) -> None:
    """리뷰 L-4: 같은 enrollment 동시 등록 → PK 위반 500이 아니라 409."""
    enrollment = site_a.enroll()
    admin = central.as_role("admin")
    responses = central.concurrently(
        *(Request("POST", "/api/v1/sites", enrollment, admin) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert [r["n"] for r in central.fetch("SELECT count(*) AS n FROM site_tokens")] == [1]


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
