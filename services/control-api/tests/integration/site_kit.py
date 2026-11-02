"""통합 테스트용 현장 쪽 재료: 실제 Updater 코드로 만든 enrollment·report export 파일,
온라인 서명 봉투, 게시용 `.pib`(서명은 가짜 — control-api는 manifest만 읽고 서명 확인은
현장 Updater 몫).

enrollment·폐쇄망 보고는 `pureiron_updater.enroll`이 쓰는 파일 그대로를 보낸다 — control-api가
r4 현장 형식을 받는지 여기서 같이 확인된다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.manifest import MANIFEST_FILE, SITE_IMAGES
from pureiron_bundle.pib import CERT_FILE, CERT_SIG, MANIFEST_SIG, write_pib
from pureiron_bundle.sitesign import (
    Json,
    SitePurpose,
    dumps,
    pack_draft,
    sign_document,
    tree_digest,
)
from pureiron_updater.enroll import enroll
from pureiron_updater.metrics import REPORTS_DIR
from pureiron_updater.outbox import export_reports
from pureiron_updater.settings import Settings as UpdaterSettings

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class Site:
    def __init__(self, tmp: Path, site_id: str) -> None:
        self.site_id = site_id
        root = tmp / f"site-{site_id}"
        self.settings = UpdaterSettings(
            site_id=site_id,
            root=root,
            root_pubkey_file=root / "root.pub",
            core_url="http://127.0.0.1:9/api/v1",
            env_file=root / "updater.env",
        )
        self._queued = 0

    def enroll(self, *, re_enroll: bool = False, next_seq: int | None = None) -> bytes:
        path = enroll(self.settings, self.site_id, now=T0, re_enroll=re_enroll, next_seq=next_seq)
        return path.read_bytes()

    @property
    def seed(self) -> bytes:
        return keys.read_private_key(self.settings.site_key_file)

    def signed(self, purpose: SitePurpose, payload: dict[str, Json]) -> bytes:
        return dumps(sign_document(self.seed, purpose, {"site_id": self.site_id, **payload}))

    def heartbeat(
        self, seq: int, *, gen: int | None, at: datetime = T0, disk_state: str = "ok"
    ) -> bytes:
        current: Json = (
            None
            if gen is None
            else {
                "product": "2026.11.0",
                "project_version": "v3",
                "gen": gen,
            }
        )
        return self.signed(
            SitePurpose.HEARTBEAT,
            {
                "report_seq": seq,
                "current": current,
                "updater_phase": "idle",
                "disk_state": disk_state,
                "at": iso(at),
            },
        )

    def report(self, seq: int, *, to: int, result: str = "succeeded") -> bytes:
        return self.signed(SitePurpose.REPORT, {**_attempt(to, result), "report_seq": seq})

    def queue_report(self, *, to: int, result: str = "succeeded") -> None:
        """Updater `metrics.write_report`와 같은 키로 `state/reports/`에 보고 하나를 쌓는다."""
        self._queued += 1
        queue = self.settings.state_dir / REPORTS_DIR
        queue.mkdir(parents=True, exist_ok=True)
        name = f"20261101T09{self._queued:02d}00000000Z-g{to}-{'0' * 12}.json"
        (queue / name).write_text(json.dumps(_attempt(to, result)), encoding="utf-8")

    def export(self, out: Path) -> bytes:
        """`pureiron-site report export`가 만드는 `site-reports` 봉투 파일."""
        exported = export_reports(self.settings, out, now=T0 + timedelta(hours=1))
        if exported is None:
            raise AssertionError("nothing queued to export")
        return exported[0].read_bytes()

    def draft(self, *, site_rev: int, base_site_rev: int, tree: bytes = b"tree-bytes") -> bytes:
        payload: dict[str, Json] = {
            "site_id": self.site_id,
            "site_rev": site_rev,
            "base_site_rev": base_site_rev,
            "content_hash": "sha256:" + "0" * 64,
            "exported_at": iso(T0),
            "project_tar_sha256": tree_digest(tree),
        }
        envelope = dumps(sign_document(self.seed, SitePurpose.DRAFT, payload))
        return pack_draft(envelope, tree)


def _attempt(to: int, result: str) -> dict[str, Json]:
    return {
        "generation": to,
        "from": to - 1,
        "to": to,
        "phase": result,
        "result": result,
        "failed_manual": False,
        "code": None if result == "succeeded" else "E_CHECK",
        "logs": "",
        "verify_s": 1.5,
        "load_s": 3.0,
        "bundle_sha256": "sha256:" + "2" * 64,
        "started_at": iso(T0),
        "ended_at": iso(T0 + timedelta(minutes=3)),
        "phase_durations_s": {"verifying": 1.5},
        "timeline": {"verifying": iso(T0)},
    }


def manifest_document(site_id: str, generation: int, *, project_version: str = "v3") -> Json:
    return {
        "format": 1,
        "kind": "site",
        "site_id": site_id,
        "generation": generation,
        "created_at": "2026-11-01T00:00:00Z",
        "product_release": {
            "version": "2026.11.0",
            "min_updater": "0.1.0",
            "images": {
                svc: {"config_digest": "sha256:" + "a" * 64, "tag": f"pureiron/{svc}:r5"}
                for svc in SITE_IMAGES
            },
            "schema": {
                "target": 5,
                "app_expected": 5,
                "app_max_compatible": 6,
                "compat_app_min": "2026.10.0",
            },
        },
        "project": {
            "project_id": "acme",
            "version": project_version,
            "content_hash": "sha256:" + "0" * 64,
        },
        "window": {
            "once": {"start": "2026-11-01T10:00:00+09:00", "end": "2026-11-01T11:00:00+09:00"}
        },
        "files": {"project/project.tar": "sha256:" + "1" * 64},
        "fleet_commit": "abc123",
    }


def build_pib(
    out: Path, site_id: str, generation: int, *, project_version: str = "v3", padding_bytes: int = 0
) -> Path:
    """게시 시험용 `.pib`(메타데이터 순서·형식은 진짜, 서명은 가짜, payload는 크기용 0 바이트)."""
    payload: dict[str, Path] = {}
    if padding_bytes:
        blob = out.parent / f"{out.name}.payload"
        blob.write_bytes(b"\0" * padding_bytes)
        payload["project/project.tar"] = blob
    manifest = manifest_document(site_id, generation, project_version=project_version)
    write_pib(
        out,
        {
            MANIFEST_FILE: json.dumps(manifest).encode(),
            MANIFEST_SIG: b"\1" * 64,
            CERT_FILE: b"{}",
            CERT_SIG: b"\2" * 64,
        },
        payload,
    )
    return out
