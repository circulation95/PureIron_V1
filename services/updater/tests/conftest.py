"""tests/ 아래 공용 fake(`updater_fakes`)를 경로로 import 한다(pytest importlib 모드).

모든 테스트는 env 파일을 `tmp_path/etc/updater.env`로 본다(R2-L3) — 개발 머신·CI 러너의 실제
`/etc/pureiron/updater.env`(Windows면 `C:\\etc\\...`)를 읽지도 쓰지도 않게. 테스트가 직접
`PUREIRON_ENV_FILE`을 다시 주면 그 값이 이긴다.
"""

import sys
from pathlib import Path

import pytest
from pureiron_updater import settings as updater_settings

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def isolated_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_file = tmp_path / "etc" / "updater.env"
    monkeypatch.setenv("PUREIRON_ENV_FILE", str(env_file))
    monkeypatch.setattr(updater_settings, "ENV_FILE", env_file)
    return env_file
