"""api 공통 오류 응답: site-db 장애 503(plan M3b-1 리뷰 A-2).

응답 detail은 고정 문자열이다 — DB 오류 원문(내부 주소·드라이버 메시지)은 인증 없는 표면(login·
`GET /project`)에서도 밖으로 나가지 않게 로그에만 남긴다.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

log = logging.getLogger(__name__)

SITE_DB_UNAVAILABLE = "site-db unavailable"


def site_db_unavailable(exc: Exception) -> HTTPException:
    log.warning("request failed, site-db unavailable: %s", exc)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SITE_DB_UNAVAILABLE)
