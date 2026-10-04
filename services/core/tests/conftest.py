from __future__ import annotations

from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
MIGRATIONS = CORE / "migrations"


@pytest.fixture
def migrations_dir() -> Path:
    return MIGRATIONS


def write_migrations(directory: Path, files: dict[str, str], epochs: dict[int, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8", newline="\n")
    lines = ["epochs:"] + [f"  {epoch}: {name}" for epoch, name in sorted(epochs.items())]
    (directory / "epochs.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return directory
