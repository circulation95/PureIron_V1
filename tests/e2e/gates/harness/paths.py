"""Windows 호스트 쪽 경로 — 저장소, e2e 작업 디렉터리(`PUREIRON_E2E_ROOT`), WSL 빌드 사본.

다른 모듈은 `paths.BUNDLES`처럼 모듈 속성으로 읽는다(단위 시험이 바꿔 끼운다).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
E2E = Path(os.environ.get("PUREIRON_E2E_ROOT", r"C:\PureIron_e2e"))
BUNDLES = E2E / "bundles"
SSH_DIR = E2E / "ssh"
"""e2e 전용 ssh 키(`golden.ensure_ssh_key`) — 모든 현장 VM이 같은 키를 쓴다."""
WSL_HOME = "$HOME/m2d-e2e"
"""WSL runner의 작업 디렉터리(저장소 사본 `repo`, 번들 재료 `work`)."""
