"""현장 compose 정적 규칙(PD-19, ADR-019/023)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, cast

from images import read_lock
from ruamel.yaml import YAML

DEPLOY = Path(__file__).resolve().parents[1]
COMPOSE = DEPLOY / "compose" / "site.compose.yml"
LOCK = DEPLOY / "releases" / "site-db.lock"


class _Loader(Protocol):
    def load(self, stream: str) -> object: ...


def _compose() -> dict[str, object]:
    loader = cast(_Loader, YAML(typ="safe", pure=True))
    return cast(dict[str, object], loader.load(COMPOSE.read_text(encoding="utf-8")))


def _services() -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], _compose()["services"])


def _lock() -> dict[str, str]:
    return read_lock(LOCK)


def test_project_name_is_fixed() -> None:
    assert _compose()["name"] == "pureiron"


def test_every_image_is_local_pureiron_with_pull_never() -> None:
    for name, service in _services().items():
        image = str(service["image"])
        assert image.startswith("pureiron/"), name
        assert service["pull_policy"] == "never", name


def test_no_digest_references_in_compose() -> None:
    assert "@sha256" not in COMPOSE.read_text(encoding="utf-8")


def test_simulator_only_in_dev_profile() -> None:
    services = _services()
    assert services["simulator"]["profiles"] == ["dev"]
    assert all("profiles" not in s for n, s in services.items() if n != "simulator")


def test_site_db_tag_matches_lock() -> None:
    lock = _lock()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["digest"])
    assert lock["upstream"].startswith("timescale/timescaledb:")
    assert lock["upstream"].endswith(lock["version"])
    image = str(_services()["site-db"]["image"])
    assert image == f"pureiron/site-db:${{PUREIRON_SITE_DB_VERSION:-{lock['version']}}}"


def test_secrets_come_from_files_only() -> None:
    secrets = cast(dict[str, dict[str, str]], _compose()["secrets"])
    assert set(secrets) == {"db_password", "updater_token"}
    for spec in secrets.values():
        assert spec["file"].startswith("${PUREIRON_SECRETS_DIR:-/var/lib/pureiron/state/secrets}/")
    text = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:" not in text
