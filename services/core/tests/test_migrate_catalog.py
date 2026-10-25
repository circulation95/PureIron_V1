from __future__ import annotations

from pathlib import Path

import pytest

from pureiron_core.platform.migrate import (
    CatalogError,
    Envelope,
    MigrationError,
    checksum_of,
    load_catalog,
)

from .conftest import write_migrations

BASE = {
    "0001_a.sql": "CREATE TABLE a (x int);",
    "0002_b.sql": "-- pureiron:no-transaction\nCREATE TABLE IF NOT EXISTS b (x int);",
    "0003_c.sql": "CREATE TABLE c (x int);",
}


def test_real_catalog_epochs(migrations_dir: Path) -> None:
    catalog = load_catalog(migrations_dir)
    assert [m.filename for m in catalog.migrations] == [
        "0001_schema_meta.sql",
        "0002_tags_devices.sql",
        "0003_tag_history.sql",
        "0004_cagg_1m.sql",
        "0005_project_state.sql",
        "0006_cagg_1h.sql",
        "0007_cagg_1h_policy.sql",
        "0008_definitions.sql",
        "0009_historian.sql",
        "0010_auth.sql",
        "0011_audit.sql",
        "0012_alarms.sql",
        "0013_project_files.sql",
    ]
    assert catalog.epochs == {1: 5, 2: 7, 3: 9, 4: 12, 5: 13}
    assert (catalog.app_expected, catalog.app_max_compatible) == (5, 6)
    assert [m.transactional for m in catalog.migrations] == [
        True,
        True,
        True,
        False,
        True,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    ]


def test_epoch_lookup_and_reached(tmp_path: Path) -> None:
    catalog = load_catalog(write_migrations(tmp_path, BASE, {1: "0002_b.sql", 2: "0003_c.sql"}))
    assert [catalog.epoch_of(v) for v in (1, 2, 3)] == [1, 1, 2]
    assert catalog.epoch_reached(set()) is None
    assert catalog.epoch_reached({1}) is None
    assert catalog.epoch_reached({1, 2}) == 1
    assert catalog.epoch_reached({1, 2, 3}) == 2
    assert catalog.last_version_of(2) == 3
    with pytest.raises(MigrationError):
        catalog.last_version_of(3)


def test_checksum_ignores_line_endings() -> None:
    assert checksum_of("a;\r\nb;\r\n") == checksum_of("a;\nb;\n")
    assert checksum_of("a;") != checksum_of("b;")


@pytest.mark.parametrize(
    ("files", "epochs", "message"),
    [
        ({**BASE, "0003_dup.sql": "SELECT 1;"}, {1: "0003_c.sql"}, "duplicate migration version"),
        ({**BASE, "3_bad.sql": "SELECT 1;"}, {1: "0003_c.sql"}, "NNNN_lower_snake"),
        (BASE, {1: "0002_b.sql"}, "not covered by any epoch"),
        (BASE, {1: "0009_missing.sql"}, "missing file"),
        (BASE, {1: "0001_a.sql", 3: "0003_c.sql"}, "contiguous"),
        (BASE, {1: "0002_b.sql", 2: "0001_a.sql", 3: "0003_c.sql"}, "must end after"),
    ],
)
def test_catalog_rejects_inconsistent_layout(
    tmp_path: Path, files: dict[str, str], epochs: dict[int, str], message: str
) -> None:
    with pytest.raises(CatalogError, match=message):
        load_catalog(write_migrations(tmp_path, files, epochs))


def test_epochs_file_must_map_ints_to_names(tmp_path: Path) -> None:
    write_migrations(tmp_path, BASE, {1: "0003_c.sql"})
    (tmp_path / "epochs.yaml").write_text("epochs:\n  one: 0003_c.sql\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="entries must be"):
        load_catalog(tmp_path)


@pytest.mark.parametrize(
    ("cur", "ok"), [(None, False), (0, False), (1, True), (2, True), (3, False)]
)
def test_envelope(cur: int | None, ok: bool) -> None:
    assert Envelope(cur=cur, app_expected=1, app_max_compatible=2).ok is ok
