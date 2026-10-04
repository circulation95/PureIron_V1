"""deploy/build·deploy/bundle은 패키지가 아닌 스크립트 모음이라 테스트에서 경로로 import 한다."""

import sys
from pathlib import Path

for _directory in ("build", "bundle"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / _directory))
