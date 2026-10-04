"""tests/ 아래 공용 fake(`updater_fakes`)를 경로로 import 한다(pytest importlib 모드)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
