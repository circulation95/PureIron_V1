"""core 실행 설정. 환경변수에서 읽고 비밀값은 파일 경로로만 받는다(PD-4 secret 파일)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pureiron_core import __version__

_SOURCE_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


class SettingsError(ValueError):
    pass


def _int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None


def _positive_int(environ: Mapping[str, str], key: str, default: int) -> int:
    value = _int(environ, key, default)
    if value <= 0:
        raise SettingsError(f"{key} must be positive, got {value}")
    return value


def _float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{key} must be a number, got {raw!r}") from None
    # float()은 "nan"·"inf"도 받는다 — 주기·제한 시간이 무한이면 그 일이 멈춘다(예: flush_s=inf면
    # 정지할 때만 flush, R1 L11).
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
    grpc_host: str
    grpc_port: int
    release: str
    migrations_dir: Path
    updater_token_file: Path | None
    # 정의 rev(= plan_rev) 확인 주기. 바뀌면 plan을 다시 만들어 collector 세션에 보낸다.
    plan_refresh_s: float = 2.0
    # 샘플 시각이 이보다 오래된 live batch 샘플은 이력에만 기록(현재값·알람 갱신 없음).
    ingest_live_max_age_s: float = 5.0
    # 장비 무신호 판정 = max(3 * poll_ms, 이 값).
    device_silence_min_s: float = 5.0
    # collector가 이 시간 동안 아무것도(heartbeat 포함) 보내지 않으면 세션을 닫는다(half-open 방지).
    # collector `PUREIRON_COLLECTOR_HEARTBEAT_S`의 2배 이상이어야 한다.
    session_silence_s: float = 15.0
    # 이력 메모리 버퍼 상한(넘치면 가장 오래된 행부터 버림). DB 장애 동안 이 버퍼가 이력을 든다.
    historian_buffer_max_rows: int = 600_000
    # 이력 flush 주기와 INSERT 한 번에 쓰는 최대 행 수.
    historian_flush_s: float = 1.0
    historian_flush_max_rows: int = 20_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        return cls(
            db_dsn=environ.get("PUREIRON_DB_DSN", "postgresql://pureiron@localhost:5432/pureiron"),
            db_password_file=_path(environ, "PUREIRON_DB_PASSWORD_FILE"),
            http_host=environ.get("PUREIRON_HTTP_HOST", "127.0.0.1"),
            http_port=_int(environ, "PUREIRON_HTTP_PORT", 8000),
            grpc_host=environ.get("PUREIRON_GRPC_HOST", "127.0.0.1"),
            grpc_port=_int(environ, "PUREIRON_GRPC_PORT", 50051),
            release=environ.get("PUREIRON_RELEASE", __version__),
            migrations_dir=_path(environ, "PUREIRON_MIGRATIONS_DIR") or _SOURCE_MIGRATIONS,
            updater_token_file=_path(environ, "PUREIRON_UPDATER_TOKEN_FILE"),
            plan_refresh_s=_float(environ, "PUREIRON_PLAN_REFRESH_S", 2.0),
            ingest_live_max_age_s=_float(environ, "PUREIRON_INGEST_LIVE_MAX_AGE_S", 5.0),
            device_silence_min_s=_float(environ, "PUREIRON_DEVICE_SILENCE_MIN_S", 5.0),
            session_silence_s=_float(environ, "PUREIRON_SESSION_SILENCE_S", 15.0),
            historian_buffer_max_rows=_positive_int(
                environ, "PUREIRON_HISTORIAN_BUFFER_MAX_ROWS", 600_000
            ),
            historian_flush_s=_float(environ, "PUREIRON_HISTORIAN_FLUSH_S", 1.0),
            historian_flush_max_rows=_positive_int(
                environ, "PUREIRON_HISTORIAN_FLUSH_MAX_ROWS", 20_000
            ),
        )
