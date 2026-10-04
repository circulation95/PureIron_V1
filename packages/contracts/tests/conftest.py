"""packages/contracts/tools는 패키지가 아닌 스크립트 모음이라 테스트에서 경로로 import 한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
