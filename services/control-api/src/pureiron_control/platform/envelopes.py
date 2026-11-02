"""site 서명 봉투에서 서명 확인 전 `payload.site_id`만 꺼낸다(경로 `{site}`와 다른지 403 판정용).

여기서 읽은 값은 믿지 않는다 — 거부(403)에만 쓰고, 받아들이는 판단은 언제나 등록된 site 키로
`sitesign.open_document`·`open_draft`가 검증한 payload로 한다.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import cast

from pureiron_bundle.sitesign import DRAFT_ENVELOPE
from pureiron_bundle.tarsafe import PARSER_EXHAUSTION, open_tar_stream

ENVELOPE_LIMIT_BYTES = 64 * 1024


def claimed_site(data: bytes) -> str | None:
    """JSON 봉투의 `payload.site_id`(없거나 형식이 틀리면 None — 서명 검증이 거부한다)."""
    try:
        envelope: object = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    payload = cast(dict[str, object], envelope).get("payload")
    if not isinstance(payload, dict):
        return None
    site = cast(dict[str, object], payload).get("site_id")
    return site if isinstance(site, str) else None


def claimed_draft_site(data: bytes) -> str | None:
    """`draft.tar` 안 `draft.json` 봉투의 `payload.site_id`."""
    try:
        with open_tar_stream(io.BytesIO(data)) as tar:
            for member in tar:
                if member.name != DRAFT_ENVELOPE:
                    continue
                if not member.isreg() or member.size > ENVELOPE_LIMIT_BYTES:
                    return None
                handle = tar.extractfile(member)
                return None if handle is None else claimed_site(handle.read())
    except (tarfile.TarError, ValueError, UnicodeError, *PARSER_EXHAUSTION):
        return None
    return None
