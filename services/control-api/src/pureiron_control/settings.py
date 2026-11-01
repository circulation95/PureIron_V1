"""control-api 실행 설정. 환경변수에서 읽고 비밀값은 파일 경로로만 받는다(PD-4).

| 변수 | 기본 | 내용 |
|---|---|---|
| `PUREIRON_CONTROL_DB_DSN` | `postgresql://pureiron@localhost:5432/pureiron_control` | DB |
| `PUREIRON_CONTROL_DB_PASSWORD_FILE` | (없음) | DB 비밀번호 파일 |
| `PUREIRON_CONTROL_HTTP_HOST`·`_PORT` | `127.0.0.1`·`8100` | HTTP 바인드(앞단 Caddy가 TLS) |
| `PUREIRON_CONTROL_DATA_DIR` | `/var/lib/pureiron-control` | `bundles/<site>/g<gen>.pib`·drafts |
| `PUREIRON_CONTROL_POLL_S` | `60` | 현장 poll 주기 — online = last_seen <= 3*poll(C8) |
| `PUREIRON_CONTROL_MAX_BUNDLE_BYTES` | `17179869184`(16 GiB) | `PUT /bundles` 업로드 상한 |
| `PUREIRON_CONTROL_MIGRATIONS_DIR` | 소스 트리 `migrations/` | 이미지에서는 설치 경로 |
| `PUREIRON_RELEASE` | 패키지 버전 | `schema_migrations.release` 기록값 |
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pureiron_control import __version__

_SOURCE_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_MAX_BUNDLE_BYTES = 16 * 1024**3


class SettingsError(ValueError):
    pass


def _positive_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{key} must be positive, got {value}")
    return value


def _positive_float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{key} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"{key} must be a positive finite number, got {raw!r}")
    return value


def _path(environ: Mapping[str, str], key: str) -> Path | None:
    raw = environ.get(key)
    return Path(raw) if raw else None


def read_secret(path: Path | None) -> str | None:
    """비밀 파일 내용(끝 줄바꿈 제거). 경로가 없으면 None, 파일이 없으면 오류."""
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SettingsError(f"cannot read secret file {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    db_dsn: str
    db_password_file: Path | None
    http_host: str
    http_port: int
    data_dir: Path
    release: str
    migrations_dir: Path
    poll_s: float = 60.0
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        return cls(
            db_dsn=environ.get(
                "PUREIRON_CONTROL_DB_DSN", "postgresql://pureiron@localhost:5432/pureiron_control"
            ),
            db_password_file=_path(environ, "PUREIRON_CONTROL_DB_PASSWORD_FILE"),
            http_host=environ.get("PUREIRON_CONTROL_HTTP_HOST", "127.0.0.1"),
            http_port=_positive_int(environ, "PUREIRON_CONTROL_HTTP_PORT", 8100),
            data_dir=_path(environ, "PUREIRON_CONTROL_DATA_DIR")
            or Path("/var/lib/pureiron-control"),
            release=environ.get("PUREIRON_RELEASE", __version__),
            migrations_dir=_path(environ, "PUREIRON_CONTROL_MIGRATIONS_DIR") or _SOURCE_MIGRATIONS,
            poll_s=_positive_float(environ, "PUREIRON_CONTROL_POLL_S", 60.0),
            max_bundle_bytes=_positive_int(
                environ, "PUREIRON_CONTROL_MAX_BUNDLE_BYTES", DEFAULT_MAX_BUNDLE_BYTES
            ),
        )
