"""코드에서 OpenAPI 계약을 결정적으로 export 한다(contracts-drift가 재실행 diff 0을 검사).

- core: `create_app(SiteRuntime.build(기본 Settings))` — 조립만 하고 DB·gRPC에 연결하지 않는다.
- control-api: `create_app(ControlRuntime.build(기본 Settings))` — 마찬가지로 조립만 한다.
출력은 키 정렬·2칸 들여쓰기·LF·끝 줄바꿈으로 고정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from pureiron_control.app import create_app as create_control_app
from pureiron_control.runtime import ControlRuntime
from pureiron_control.settings import Settings as ControlSettings
from pureiron_core.app import create_app as create_core_app
from pureiron_core.runtime import SiteRuntime
from pureiron_core.settings import Settings

OUT = Path(__file__).resolve().parents[1] / "openapi"


def _dump(app: FastAPI) -> str:
    return json.dumps(app.openapi(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    documents = {
        "core.json": _dump(create_core_app(SiteRuntime.build(Settings.from_env({})))),
        "control-api.json": _dump(
            create_control_app(ControlRuntime.build(ControlSettings.from_env({})))
        ),
    }
    for name, text in documents.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
