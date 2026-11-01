"""api 공통 오류: control-db 장애 503. detail은 고정 문자열이다(DB 오류 원문은 로그에만)."""

from __future__ import annotations

import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

CONTROL_DB_UNAVAILABLE = "control-db unavailable"


async def control_db_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    log.warning("request failed, control-db unavailable: %s", exc)
    return JSONResponse(
        {"detail": CONTROL_DB_UNAVAILABLE}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
    )
