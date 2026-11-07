"""중앙 release 기록(`deploy/releases/central/`)은 현장 기록 소비자에게 보이지 않는다(M4-3 G5·G10).

현장 기록을 읽는 곳 — 번들 빌더(`load_releases`, check-gen·publish·revert가 같이 씀)와
n-minus-1(`load_records`) — 은 `deploy/releases/*.json`만 본다. 중앙 기록이 같은 형식이어도
하위 디렉터리라 현장 manifest·N-1 선택에 섞이지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import n_minus_1
import release_record
from build_bundle import load_releases

from pureiron_bundle.manifest import SITE_IMAGES
from pureiron_control.cli import CENTRAL_RELEASES_DIR

DEPLOY = Path(__file__).resolve().parents[1]


def _site_record(name: str, epoch: int) -> dict[str, object]:
    image = {"tag": f"pureiron/core-test:{name}", "config_digest": "sha256:" + "1" * 64}
    return {
        "name": name,
        "release": name,
        "version": "2026.11.0",
        "images": {"core-test": image},
        "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
    }


def _central_record(name: str) -> dict[str, object]:
    """중앙 기록이 현장 기록 필드(version·core-test)를 흉내 내도 읽히지 않아야 한다."""
    return {**_site_record(name, 9), "kind": "central"}


def _layout(tmp_path: Path) -> Path:
    releases = tmp_path / "releases"
    (releases / "central").mkdir(parents=True)
    (releases / "r4.json").write_text(json.dumps(_site_record("r4", 5)), encoding="utf-8")
    central = json.dumps(_central_record("c0"))
    (releases / "central" / "c0.json").write_text(central, encoding="utf-8")
    return releases


def test_bundle_builder_ignores_central_records(tmp_path: Path) -> None:
    assert [r.name for r in load_releases(_layout(tmp_path))] == ["r4"]


def test_n_minus_1_ignores_central_records(tmp_path: Path) -> None:
    assert [r.name for r in n_minus_1.load_records(_layout(tmp_path))] == ["r4"]


def test_central_record_location_is_shared() -> None:
    """release_record가 쓰는 곳 = control-api migrate lint가 읽는 곳."""
    assert DEPLOY.parent / CENTRAL_RELEASES_DIR == release_record.CENTRAL_RELEASES
    assert release_record.CENTRAL_RELEASES.parent == release_record.RELEASES


def test_central_services_are_not_site_images() -> None:
    assert set(release_record.CENTRAL_SERVICES).isdisjoint(SITE_IMAGES)
    assert set(release_record.CENTRAL_SERVICES).isdisjoint(release_record.RECORD_SERVICES)
