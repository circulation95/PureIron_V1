from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pureiron_core.audit.ports import AuditEntry, AuditQuery, AuditRecord
from pureiron_core.project.ports import Definitions, DeviceDef, TagDef

CORE = Path(__file__).resolve().parents[1]
MIGRATIONS = CORE / "migrations"


@pytest.fixture
def migrations_dir() -> Path:
    return MIGRATIONS


@dataclass
class FakeAudit:
    """`AuditLog` 가짜: 기록을 모으고, 조회는 받은 조건과 미리 넣은 결과를 돌려준다."""

    entries: list[AuditEntry] = field(default_factory=list[AuditEntry])
    queries: list[AuditQuery] = field(default_factory=list[AuditQuery])
    results: tuple[AuditRecord, ...] = ()

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    async def query(self, query: AuditQuery) -> tuple[AuditRecord, ...]:
        self.queries.append(query)
        return self.results


def write_migrations(directory: Path, files: dict[str, str], epochs: dict[int, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8", newline="\n")
    lines = ["epochs:"] + [f"  {epoch}: {name}" for epoch, name in sorted(epochs.items())]
    (directory / "epochs.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return directory


def sample_definitions(rev: int = 3) -> Definitions:
    """장비 1대(plc-1) + 태그 2개(float32 holding, bool coil). 정의는 정규형(기본값 생략)."""
    return Definitions(
        rev=rev,
        devices=(
            DeviceDef(
                "plc-1",
                {"id": "plc-1", "protocol": "modbus_tcp", "host": "10.0.0.5", "port": 502},
            ),
        ),
        tags=(
            TagDef(
                1,
                "line1/temp",
                "plc-1",
                {
                    "path": "line1/temp",
                    "device": "plc-1",
                    "modbus": {"register_type": "holding", "address": 0, "data_type": "float32"},
                },
            ),
            TagDef(
                2,
                "line1/running",
                "plc-1",
                {
                    "path": "line1/running",
                    "device": "plc-1",
                    "modbus": {"register_type": "coil", "address": 2, "data_type": "bool"},
                },
            ),
        ),
    )
