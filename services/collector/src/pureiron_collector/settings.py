"""collector 실행 설정(환경변수). collector는 DB 설정을 갖지 않는다(무상태·DB 무접근)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class SettingsError(ValueError):
    pass


def _float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{key} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{key} must be positive, got {raw!r}")
    return value


def _int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{key} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class CollectorSettings:
    core_target: str
    heartbeat_s: float
    reconnect_initial_s: float
    # 재접속 간격 상한. 링 보관 창(60s)보다 훨씬 작아야 한다 — 0.5s부터 두 배씩 30s까지 늘면 35s
    # 단절 뒤 다음 시도가 ~66s에 와서 링 창 밖으로 밀린 분리 구간 앞부분을 잃는다(M3a-3 실측).
    reconnect_max_s: float
    # ingest 의미론(plan B9): 100ms 또는 500값마다 batch, Ack 전 60s 보관.
    batch_interval_s: float = 0.1
    batch_max_samples: int = 500
    ring_window_s: float = 60.0
    health_interval_s: float = 2.0
    # core가 이 시간 동안 아무것도(Ack 포함) 보내지 않으면 끊긴 세션으로 보고 재연결한다.
    core_silence_s: float = 5.0

    def __post_init__(self) -> None:
        if self.heartbeat_s * 2 > self.core_silence_s:
            raise SettingsError(
                f"PUREIRON_COLLECTOR_CORE_SILENCE_S ({self.core_silence_s}) must be at least twice "
                f"PUREIRON_COLLECTOR_HEARTBEAT_S ({self.heartbeat_s})"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> CollectorSettings:
        return cls(
            core_target=environ.get("PUREIRON_CORE_GRPC", "127.0.0.1:50051"),
            heartbeat_s=_float(environ, "PUREIRON_COLLECTOR_HEARTBEAT_S", 1.0),
            reconnect_initial_s=_float(environ, "PUREIRON_COLLECTOR_RECONNECT_INITIAL_S", 0.5),
            reconnect_max_s=_float(environ, "PUREIRON_COLLECTOR_RECONNECT_MAX_S", 5.0),
            batch_interval_s=_float(environ, "PUREIRON_COLLECTOR_BATCH_INTERVAL_S", 0.1),
            batch_max_samples=_int(environ, "PUREIRON_COLLECTOR_BATCH_MAX_SAMPLES", 500),
            ring_window_s=_float(environ, "PUREIRON_COLLECTOR_RING_WINDOW_S", 60.0),
            health_interval_s=_float(environ, "PUREIRON_COLLECTOR_HEALTH_INTERVAL_S", 2.0),
            core_silence_s=_float(environ, "PUREIRON_COLLECTOR_CORE_SILENCE_S", 5.0),
        )
