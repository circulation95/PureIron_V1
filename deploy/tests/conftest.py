"""deploy/build·bundle·simulator는 패키지가 아닌 스크립트 모음이라 테스트에서 경로로 import 한다.

`deploy/fleet`은 패키지(`fleet.*`)라 그 부모 `deploy/`를 넣는다(fleet 진입 스크립트와 같은 방식).
"""

import sys
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1]
for _directory in ("build", "bundle", "simulator"):
    sys.path.insert(0, str(_DEPLOY / _directory))
sys.path.insert(0, str(_DEPLOY))
