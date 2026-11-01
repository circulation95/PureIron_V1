"""요청 본문 읽기(상한 413)와 OpenAPI 요청 본문 선언.

서명 봉투는 받은 바이트 그대로 검증해야 하므로(`sitesign.open_document`) pydantic 본문 모델로 받지
않고 원문을 읽는다. 대신 OpenAPI에는 봉투 스키마를 `openapi_extra`로 싣는다(계약 정본).
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

ENVELOPE_DESCRIPTION = (
    "현장 site key 서명 봉투(`pureiron_bundle.sitesign`). 서명 대상은 JCS(`{format, payload, "
    "purpose}`), 서명은 Ed25519 raw 64B hex."
)
ENVELOPE_SCHEMA: dict[str, object] = {
    "type": "object",
    "title": "SiteSignedEnvelope",
    "required": ["format", "purpose", "payload", "signature"],
    "additionalProperties": False,
    "properties": {
        "format": {"const": 1},
        "purpose": {"type": "string"},
        "payload": {"type": "object"},
        "signature": {"type": "string", "pattern": "^[0-9a-f]{128}$"},
    },
}


def envelope_body(purpose: str) -> dict[str, object]:
    schema = {**ENVELOPE_SCHEMA, "description": f"purpose `{purpose}` 봉투. {ENVELOPE_DESCRIPTION}"}
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def binary_body(media_type: str, description: str) -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {media_type: {"schema": {"type": "string", "format": "binary"}}},
        }
    }


async def read_body(request: Request, *, limit_bytes: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit_bytes:
        raise _too_large(limit_bytes)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit_bytes:
            raise _too_large(limit_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(limit_bytes: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, detail=f"request body exceeds {limit_bytes} bytes"
    )
