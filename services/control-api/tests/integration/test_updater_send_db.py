"""현장 송신(M4-1)을 실제 control-api(uvicorn + control-db)와 실제 Updater 코드로: 서비스 tick의
heartbeat·보고 전송, 중앙 중단 중 보고의 USB 반출(같은 seq), 드래프트 업로드와 latest 순서,
desired polling의 `last_seen`(E3), 드래프트 저장 실패.

tick은 `pureiron_updater.cli.tick` 그대로다(진행 중 시도가 없으면 docker를 부르지 않는다).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from pureiron_bundle.manifest import manifest_bytes
from pureiron_bundle.sitesign import Json
from pureiron_updater import cli
from pureiron_updater.outbox import export_reports, pending
from pureiron_updater.poll import TOKEN_FILE, ControlApi
from pureiron_updater.settings import Settings as UpdaterSettings
from pureiron_updater.state import StateStore, UpdaterState

from .conftest import Central
from .site_kit import T0, Site, manifest_document

pytestmark = pytest.mark.integration

type JsonObject = dict[str, object]
DEAD_CONTROL_API = "http://127.0.0.1:9/api/v1"


class OnlineSite:
    """등록된 현장 하나: 실제 Updater 설정(control-api = `server`)·token·적용된 g1."""

    def __init__(self, central: Central, server: str, tmp: Path, site_id: str = "site-a") -> None:
        self.central = central
        self.kit = Site(tmp, site_id)
        registered = central.client.post(
            "/api/v1/sites", content=self.kit.enroll(), headers=central.as_role("admin")
        )
        assert registered.status_code == 201, registered.text
        self.settings: UpdaterSettings = replace(self.kit.settings, control_api=server)
        token = self.settings.secrets_dir / TOKEN_FILE
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(registered.json()["token"] + "\n", encoding="utf-8")

    def apply_g1(self) -> None:
        """g1이 적용된 상태(Updater가 남기는 `updater.json`·`compose/g1/manifest.json`)."""
        target = self.settings.compose_dir / "g1"
        target.mkdir(parents=True)
        document = cast(dict[str, Json], manifest_document(self.kit.site_id, 1))
        (target / "manifest.json").write_bytes(manifest_bytes(document))
        StateStore(self.settings.state_dir).save(UpdaterState(applied_gen=1))

    def api(self, base: str | None = None) -> ControlApi:
        token = self.settings.secrets_dir / TOKEN_FILE
        return ControlApi(base or cast(str, self.settings.control_api), self.kit.site_id, token)

    def tick(self, base: str | None = None) -> None:
        settings = replace(self.settings, control_api=base or self.settings.control_api)
        cli.tick(settings, self.api(base), first=False)

    def fleet(self) -> JsonObject:
        response = self.central.client.get(
            f"/api/v1/fleet?site={self.kit.site_id}", headers=self.central.as_role("viewer")
        )
        assert response.status_code == 200, response.text
        (row,) = cast(list[JsonObject], response.json())
        return row

    def seqs(self, table: str) -> list[tuple[int, str, str]]:
        rows = self.central.fetch(
            f"SELECT report_seq, source, outcome FROM {table} WHERE site_id = $1 ORDER BY id",
            self.kit.site_id,
        )
        return [(r["report_seq"], r["source"], r["outcome"]) for r in rows]


def test_service_tick_sends_heartbeat_and_queued_reports(
    central: Central, server: str, tmp_path: Path
) -> None:
    site = OnlineSite(central, server, tmp_path)
    site.apply_g1()
    site.tick()
    row = site.fleet()
    assert row["connection"] == "online"
    current = cast(JsonObject, row["current"])
    assert (current["product"], current["project_version"], current["gen"]) == (
        "2026.11.0",
        "v3",
        1,
    )
    assert current["source"] == "online"
    assert row["disk_state"] in {"ok", "warn", "critical"}
    assert row["updater_phase"] == "idle"
    assert row["last_heartbeat_seq"] == 1

    # 보고 2건 → 다음 tick: heartbeat(2) 다음 보고(3·4) — 카운터는 공용, 수위는 종류별.
    site.kit.queue_report(to=1, result="failed")
    site.kit.queue_report(to=1)
    site.tick()
    assert pending(site.settings.state_dir) == []
    assert len(list((site.settings.state_dir / "reports" / "exported").glob("*.json"))) == 2
    row = site.fleet()
    attempt = cast(JsonObject, row["last_attempt"])
    assert (attempt["report_seq"], attempt["result"], attempt["source"]) == (
        4,
        "succeeded",
        "online",
    )
    assert (row["last_heartbeat_seq"], row["last_report_seq"]) == (2, 4)
    assert site.seqs("heartbeats") == [(1, "online", "applied"), (2, "online", "applied")]
    assert site.seqs("apply_attempts") == [(3, "online", "applied"), (4, "online", "applied")]
    prom = (site.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert "updater_reports_pending 0" in prom


def test_report_held_while_central_is_down_keeps_its_seq_through_usb(
    central: Central, server: str, tmp_path: Path
) -> None:
    """N-b: 중앙이 멈춘 동안 seq를 받은 보고는 USB 반출·반입에서도, 뒤늦은 온라인 재전송에서도 같은
    seq다 — 중앙은 한 번 반영(applied)하고 나머지는 이력(history)으로 받는다."""
    site = OnlineSite(central, server, tmp_path)
    site.tick()  # heartbeat 1
    site.kit.queue_report(to=1)
    site.tick(DEAD_CONTROL_API)  # heartbeat 2 버림, 보고는 seq 3을 받고 큐에 남는다
    (held,) = pending(site.settings.state_dir)
    assert json.loads(held.read_text(encoding="utf-8"))["report_seq"] == 3
    copy = tmp_path / held.name
    shutil.copyfile(held, copy)

    exported = export_reports(site.settings, tmp_path / "usb", now=T0 + timedelta(hours=1))
    assert exported is not None
    assert exported[0].name == "pureiron-reports-site-a-000003-000003.json"
    imported = central.client.post(
        "/api/v1/offline-imports",
        content=exported[0].read_bytes(),
        headers=central.as_role("admin"),
    )
    assert imported.status_code == 201, imported.text
    assert (imported.json()["applied_total"], imported.json()["last_seq"]) == (1, 3)

    # 응답을 못 받았던 온라인 전송이 같은 파일로 다시 나가도(같은 seq) 이력일 뿐이다.
    copy.replace(held)
    site.tick()
    assert pending(site.settings.state_dir) == []
    assert site.seqs("apply_attempts") == [(3, "offline", "applied"), (3, "online", "history")]
    attempt = cast(JsonObject, site.fleet()["last_attempt"])
    assert (attempt["report_seq"], attempt["source"]) == (3, "offline")
    assert site.seqs("heartbeats") == [(1, "online", "applied"), (4, "online", "applied")]


def test_rejected_report_is_moved_aside_and_does_not_block_the_queue(
    central: Central, server: str, tmp_path: Path
) -> None:
    site = OnlineSite(central, server, tmp_path)
    queue = site.settings.state_dir / "reports"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "20261101T080000000000Z-g1-bad.json").write_text(
        json.dumps({"generation": 1, "result": ""}), encoding="utf-8"
    )  # result 빈 문자열 → 중앙 400
    site.kit.queue_report(to=1)
    site.tick()
    assert (queue / "rejected" / "20261101T080000000000Z-g1-bad.json").exists()
    assert pending(site.settings.state_dir) == []
    assert site.seqs("apply_attempts") == [(3, "online", "applied")]
    prom = (site.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert "updater_reports_rejected_total 1" in prom


def test_draft_upload_latest_is_the_highest_site_rev(
    central: Central, server: str, tmp_path: Path
) -> None:
    """`project export --upload`가 올리는 그대로(application/x-tar) — 늦게 도착한 낮은 site_rev
    재전송이 latest를 가리지 않는다(L-5)."""
    site = OnlineSite(central, server, tmp_path)
    api = site.api()
    older = site.kit.draft(site_rev=5, base_site_rev=2, tree=b"tree-5")
    newer = site.kit.draft(site_rev=7, base_site_rev=2, tree=b"tree-7")
    for data in (older, newer, older):
        status, body = api.post(api.site_path("drafts"), data, cli.DRAFT_MEDIA_TYPE)
        assert status == 201, body
    latest = central.client.get(
        "/api/v1/sites/site-a/drafts/latest", headers=central.as_role("admin")
    )
    assert latest.status_code == 200
    assert latest.headers["X-PureIron-Draft-Site-Rev"] == "7"
    assert latest.content == newer


def test_draft_storage_failure_leaves_no_row_and_no_file(
    central: Central, server: str, tmp_path: Path
) -> None:
    site = OnlineSite(central, server, tmp_path)
    blocker = central.settings.data_dir / "drafts" / "site-a"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"")  # 현장 디렉터리 자리에 파일 — 디스크 가득·권한 오류와 같은 OSError
    api = site.api()
    status, body = api.post(
        api.site_path("drafts"), site.kit.draft(site_rev=3, base_site_rev=1), cli.DRAFT_MEDIA_TYPE
    )
    assert status == 507, body
    assert json.loads(body)["detail"] == "draft storage unavailable"
    assert central.fetch("SELECT count(*) AS n FROM drafts")[0]["n"] == 0
    assert blocker.is_file()


def test_desired_polling_marks_an_r4_site_as_seen(
    central: Central, server: str, tmp_path: Path
) -> None:
    """E3: heartbeat를 보내지 않는 r4 Updater도 desired polling(게시 없음 404 포함)으로 연결이
    보인다."""
    site = OnlineSite(central, server, tmp_path)
    assert site.fleet()["connection"] == "never"
    assert site.api().desired() is None  # r4 poll.py 그대로, 404
    row = site.fleet()
    assert row["connection"] == "online"
    assert row["last_heartbeat_seq"] == 0
    central.clock.advance(181)
    assert site.fleet()["connection"] == "stale"
