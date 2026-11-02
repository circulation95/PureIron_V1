"""api 공통 오류: control-db 장애 503·site 키 불일치 409. detail은 고정 문자열이다(DB 오류 원문은
로그에만)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from pureiron_bundle import keys
from pureiron_bundle.sitesign import SitePurpose

log = logging.getLogger(__name__)

CONTROL_DB_UNAVAILABLE = "control-db unavailable"
KEY_MISMATCH = "signature does not match the registered site key"
KEY_MISMATCH_RESPONSE: dict[int | str, dict[str, object]] = {
    409: {
        "description": "등록된 site 키로 서명이 확인되지 않음(현장 재등록 뒤 rekey 대기, 또는 "
        "폐기된 구 키로 서명)"
    }
}
"""현장 서명 봉투(heartbeat·보고·드래프트·폐쇄망 반입)의 공통 응답(M4-1 리뷰 M1). 형식 위반(400)과
달리 현장은 버리지 않고 다시 보낸다 — rekey가 반영되면 같은 메시지가 받아진다. 문구는 원인을
단정하지 않는다(R2-L2): rekey 대기일 수도, 백업에서 복원한 옛 `site.key`나 rekey 전에 내보낸 USB
파일처럼 폐기된 키 서명일 수도 있다."""


def key_mismatch(
    site: str, purpose: SitePurpose, public: bytes, *, token: str | None = None
) -> HTTPException:
    """409 + 경고 로그(현장·용도·등록 키 지문, 반입이면 token 이름) — 현장이 rekey를 기다리는 것이
    중앙 로그에서 보이게(R2-L2)."""
    log.warning(
        "site %s: %s signature does not verify with the registered key %s%s",
        site,
        purpose.value,
        keys.fingerprint(public),
        f" (token {token})" if token else "",
    )
    return HTTPException(status.HTTP_409_CONFLICT, detail=KEY_MISMATCH)


async def control_db_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    log.warning("request failed, control-db unavailable: %s", exc)
    return JSONResponse(
        {"detail": CONTROL_DB_UNAVAILABLE}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
    )
