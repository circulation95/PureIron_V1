"""Updater 설정(환경변수, systemd `EnvironmentFile=/etc/pureiron/updater.env`).

운영자가 직접 부르는 `pureiron-site`도 같은 값을 보도록, 프로세스 환경에 없는 변수는
`PUREIRON_ENV_FILE`(기본 `/etc/pureiron/updater.env`)의 `KEY=VALUE` 줄에서 읽는다
(프로세스 환경 우선).
`enroll`이 이 파일에 `PUREIRON_SITE_ID`를 기록한다.

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `PUREIRON_SITE_ID` | (필수, `enroll`이 기록) | 이 현장 id — 번들 `site_id`와 같아야 한다 |
| `PUREIRON_ROOT` | `/var/lib/pureiron` | `{db,state,backups,bundles}` 상위 |
| `PUREIRON_ROOT_PUB` | `/etc/pureiron/root.pub` | root 공개키(raw 32B) |
| `PUREIRON_CORE_URL` | `http://127.0.0.1:<web port>/api/v1` | core API(web 프록시) |
| `PUREIRON_CONTROL_API` | (없음) | control-api 기준 URL. 없으면 polling·송신 안 함(폐쇄망) |
| `PUREIRON_CHECK_TIMEOUT_S` | `90` | `checking` 제한 시간 |
| `PUREIRON_POLL_INTERVAL_S` | `60` | 서비스 loop 주기 |
| `PUREIRON_WEB_BIND`·`PUREIRON_WEB_PORT` | compose 기본값 | 주면 generation `.env`에도 넣는다 |
| `PUREIRON_SECRET_UID` | `10001` | secret 파일 소유자(컨테이너 서비스 사용자, root면 chown) |

`PUREIRON_WRITES_ENABLED`는 **무시한다**(M4-1 C2): core 쓰기 스위치는 서명된 번들 manifest
(`site_config.writes_enabled` ← fleet 현장 파일 `writes.enabled`)에서만 온다. 이 파일·프로세스
환경에 있으면 서비스 시작 때 경고 1회(`ignored_env`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pureiron_updater import __version__


class SettingsError(ValueError):
    pass


_PASSTHROUGH = ("PUREIRON_WEB_BIND", "PUREIRON_WEB_PORT")
IGNORED_VARS = ("PUREIRON_WRITES_ENABLED",)
"""설정해도 쓰지 않는 변수(서명된 manifest에서만 오는 값)."""
ENV_FILE = Path("/etc/pureiron/updater.env")
SITE_ID_VAR = "PUREIRON_SITE_ID"
DEFAULT_SECRET_UID = 10001


def env_file_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("PUREIRON_ENV_FILE") or ENV_FILE)


def parse_env_file(text: str) -> dict[str, str]:
    """systemd `EnvironmentFile` 부분집합: 빈 줄·`#` 주석 무시, `KEY=VALUE`, 값 양끝 따옴표 제거."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def merged_env(env: Mapping[str, str]) -> dict[str, str]:
    """env 파일 값 위에 프로세스 환경을 덮는다(파일이 없으면 프로세스 환경 그대로)."""
    try:
        text = env_file_path(env).read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(env)
    return {**parse_env_file(text), **env}


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
    env_file: Path = ENV_FILE
    secret_uid: int = DEFAULT_SECRET_UID
    ignored_env: tuple[str, ...] = ()
    """설정됐지만 무시하는 변수(`IGNORED_VARS` 중 env 파일·프로세스 환경에 있던 것)."""

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

    @property
    def site_key_file(self) -> Path:
        return self.state_dir / "site.key"

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_site: bool = True) -> Settings:
        """`require_site=False`는 등록 전에도 쓰는 명령(enroll·secret·설치 번들 검증)용 —
        그때 `site_id`는 빈 문자열일 수 있다."""
        site_id = env.get(SITE_ID_VAR, "").strip()
        if require_site and not site_id:
            raise SettingsError(
                f"{SITE_ID_VAR} is required (run: pureiron-site enroll --site <id>)"
            )
        port = env.get("PUREIRON_WEB_PORT", "80")
        try:
            check_timeout = float(env.get("PUREIRON_CHECK_TIMEOUT_S", "90"))
            poll_interval = float(env.get("PUREIRON_POLL_INTERVAL_S", "60"))
            secret_uid = int(env.get("PUREIRON_SECRET_UID", str(DEFAULT_SECRET_UID)))
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
            env_file=env_file_path(env),
            secret_uid=secret_uid,
            ignored_env=tuple(k for k in IGNORED_VARS if k in env),
        )
