"""control-api 설정: 기본값과 잘못된 env 거부(기동 시 바로 실패)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pureiron_control.settings import DEFAULT_MAX_BUNDLE_BYTES, Settings, SettingsError, read_secret


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.db_dsn == "postgresql://pureiron@localhost:5432/pureiron_control"
    assert (settings.http_host, settings.http_port) == ("127.0.0.1", 8100)
    assert settings.poll_s == 60.0
    assert settings.max_bundle_bytes == DEFAULT_MAX_BUNDLE_BYTES == 16 * 1024**3
    assert settings.data_dir == Path("/var/lib/pureiron-control")
    assert (settings.migrations_dir / "epochs.yaml").is_file()


def test_env_overrides(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PUREIRON_CONTROL_DB_DSN": "postgresql://x@db/c",
            "PUREIRON_CONTROL_HTTP_PORT": "9000",
            "PUREIRON_CONTROL_DATA_DIR": str(tmp_path),
            "PUREIRON_CONTROL_POLL_S": "30",
            "PUREIRON_CONTROL_MAX_BUNDLE_BYTES": "1024",
            "PUREIRON_RELEASE": "0.1.0-m40",
        }
    )
    assert (settings.db_dsn, settings.http_port, settings.data_dir) == (
        "postgresql://x@db/c",
        9000,
        tmp_path,
    )
    assert (settings.poll_s, settings.max_bundle_bytes, settings.release) == (
        30.0,
        1024,
        "0.1.0-m40",
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PUREIRON_CONTROL_HTTP_PORT", "http"),
        ("PUREIRON_CONTROL_HTTP_PORT", "0"),
        ("PUREIRON_CONTROL_POLL_S", "nan"),
        ("PUREIRON_CONTROL_POLL_S", "-1"),
        ("PUREIRON_CONTROL_MAX_BUNDLE_BYTES", "1e9"),
    ],
)
def test_bad_values_fail_at_startup(key: str, value: str) -> None:
    with pytest.raises(SettingsError, match=key):
        Settings.from_env({key: value})


def test_secret_file_is_read_without_newline(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n", encoding="utf-8")
    assert read_secret(secret) == "s3cret"
    assert read_secret(None) is None
    with pytest.raises(SettingsError):
        read_secret(tmp_path / "missing")
