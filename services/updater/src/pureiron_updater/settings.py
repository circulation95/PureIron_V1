"""Updater 설정(환경변수, systemd `EnvironmentFile=/etc/pureiron/updater.env`).

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `PUREIRON_SITE_ID` | (필수) | 이 현장 id — 번들 `site_id`와 같아야 한다 |
| `PUREIRON_ROOT` | `/var/lib/pureiron` | `{db,state,backups,bundles}` 상위 |
| `PUREIRON_ROOT_PUB` | `/etc/pureiron/root.pub` | root 공개키(raw 32B) |
| `PUREIRON_CORE_URL` | `http://127.0.0.1:<web port>/api/v1` | core API(web 프록시) |
| `PUREIRON_CONTROL_API` | (없음) | control-api 기준 URL. 없으면 polling 안 함(폐쇄망: `apply`만) |
| `PUREIRON_CHECK_TIMEOUT_S` | `90` | `checking` 제한 시간 |
| `PUREIRON_POLL_INTERVAL_S` | `60` | 서비스 loop 주기 |
| `PUREIRON_WEB_BIND`·`PUREIRON_WEB_PORT` | compose 기본값 | 주면 generation `.env`에도 넣는다 |
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pureiron_updater import __version__


class SettingsError(ValueError):
    pass


_PASSTHROUGH = ("PUREIRON_WEB_BIND", "PUREIRON_WEB_PORT")


@dataclass(frozen=True, slots=True)
class Settings:
    site_id: str
    root: Path
    root_pubkey_file: Path
    core_url: str
    control_api: str | None = None
    check_timeout_s: float = 90.0
    poll_interval_s: float = 60.0
    compose_env: tuple[tuple[str, str], ...] = ()
    updater_version: str = __version__

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def bundles_dir(self) -> Path:
        return self.root / "bundles"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def compose_dir(self) -> Path:
        return self.state_dir / "compose"

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    def site_env(self) -> dict[str, str]:
        """generation `.env`에 들어가는 현장 경로·포트."""
        return {
            "PUREIRON_DB_DATA": str(self.root / "db"),
            "PUREIRON_SECRETS_DIR": str(self.secrets_dir),
            **dict(self.compose_env),
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        site_id = env.get("PUREIRON_SITE_ID", "").strip()
        if not site_id:
            raise SettingsError("PUREIRON_SITE_ID is required")
        port = env.get("PUREIRON_WEB_PORT", "80")
        try:
            check_timeout = float(env.get("PUREIRON_CHECK_TIMEOUT_S", "90"))
            poll_interval = float(env.get("PUREIRON_POLL_INTERVAL_S", "60"))
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
        return cls(
            site_id=site_id,
            root=Path(env.get("PUREIRON_ROOT", "/var/lib/pureiron")),
            root_pubkey_file=Path(env.get("PUREIRON_ROOT_PUB", "/etc/pureiron/root.pub")),
            core_url=env.get("PUREIRON_CORE_URL", f"http://127.0.0.1:{port}/api/v1"),
            control_api=env.get("PUREIRON_CONTROL_API") or None,
            check_timeout_s=check_timeout,
            poll_interval_s=poll_interval,
            compose_env=tuple((k, env[k]) for k in _PASSTHROUGH if env.get(k)),
        )
