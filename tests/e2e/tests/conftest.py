from __future__ import annotations

import sys
from pathlib import Path

E2E = Path(__file__).resolve().parents[1]
for sub in ("infra", "site", "m2d"):
    sys.path.insert(0, str(E2E / sub))
sys.path.insert(0, str(E2E.parents[1] / "deploy" / "build"))
