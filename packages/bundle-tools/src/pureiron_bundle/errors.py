"""번들 거부 코드(ADR-023, plan §6.2).

검증 6단계의 코드에 더해 `E_FORMAT`을 둔다: tar 구조(경로·멤버 종류·중복·순서·크기)나
서명 확인 뒤의 manifest·cert가 계약 형식에 맞지 않을 때다. 서명 확인 전에는 어떤 내용도
해석하지 않으므로, `E_FORMAT`은 "서명은 맞지만 형식이 틀린" 번들 또는 tar 자체가 깨진 경우다.
"""

from __future__ import annotations

from enum import StrEnum


class RejectCode(StrEnum):
    E_SIG = "E_SIG"
    """root→cert→manifest 서명 불일치(또는 trust 서명 형식 오류)."""
    E_CERT = "E_CERT"
    """cert 유효기간·serial 하한·created_at 역행."""
    E_SITE = "E_SITE"
    E_GEN = "E_GEN"
    E_DIGEST = "E_DIGEST"
    """payload sha256 불일치·누락·초과, 이미지 config digest 불일치."""
    E_SCHEMA = "E_SCHEMA"
    E_UPDATER = "E_UPDATER"
    E_DRAFT = "E_DRAFT"
    E_FORMAT = "E_FORMAT"
    """tar 구조·메타데이터 형식 오류(경로 이탈, 링크, 중복, 순서, 계약 위반 manifest)."""


class BundleRejectedError(Exception):
    """번들 거부. `code`는 보고(ApplyAttempt)에 그대로 쓰인다."""

    def __init__(self, code: RejectCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
