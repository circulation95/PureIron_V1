from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from pureiron_control import __version__


class Health(BaseModel):
    status: Literal["ok"]
    version: str


def create_app() -> FastAPI:
    app = FastAPI(title="PureIron control-api", version=__version__)

    @app.get("/api/v1/healthz", operation_id="control_healthz")
    async def healthz() -> Health:
        return Health(status="ok", version=__version__)

    return app
