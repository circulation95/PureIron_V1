"""적대적 tar 헤더 제한 — bundle-tools가 tar를 읽을 때는 항상 `open_tar_stream`을 쓴다.

CPython `tarfile`은 확장 헤더(pax `x`/`g`, GNU `L`/`K`) 본문을 크기 제한 없이 메모리로 읽고,
연속된 확장 헤더를 재귀로 처리한다(수천 개를 이어 붙이면 `RecursionError`). 서명 확인 전에
읽는 입력이므로 다음만 허용한다:

- pax 확장 헤더(`XHDTYPE`·`SOLARIS_XHDTYPE`)는 64 KiB 이하, 멤버 하나 앞에 최대 1개.
- 전역 pax 헤더(`XGLTYPE`), GNU long name/link, GNU sparse(헤더 타입·`GNU.sparse.*` pax 키)는 거부.
- pax 값 해석 중 나는 `ValueError`·`UnicodeError`도 같은 거부로 바꾼다.

제한 위반은 `UnsafeTarHeaderError`(= `tarfile.TarError`)다. `HeaderError` 계열이 아니어야
tarfile이 "아카이브 끝"으로 삼키지 않는다. 남은 파서 고갈(`RecursionError`·`MemoryError`)은
호출자가 2차 방어로 같은 거부로 바꾼다(`PARSER_EXHAUSTION`).
"""

from __future__ import annotations

import tarfile
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import IO, Protocol, cast

EXTENDED_HEADER_LIMIT = 64 * 1024
_EXTENDED = (tarfile.XHDTYPE, tarfile.SOLARIS_XHDTYPE)
_FORBIDDEN = (
    tarfile.XGLTYPE,
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
    tarfile.GNUTYPE_SPARSE,
)
PARSER_EXHAUSTION = (RecursionError, MemoryError)
_PARENT_HOOK = "_proc_member"
_APPLY_HOOK = "_apply_pax_info"
_SPARSE_PREFIX = "GNU.sparse."
_after_extended: ContextVar[bool] = ContextVar("pureiron_tar_after_extended", default=False)


class Readable(Protocol):
    """`tarfile.open(fileobj=…)`이 요구하는 파일 객체 모양(스트림 모드는 read만 실제로 쓴다)."""

    def read(self, size: int, /) -> bytes: ...

    def write(self, b: bytes, /) -> object: ...

    def tell(self) -> int: ...

    def seek(self, pos: int, /) -> object: ...

    def close(self) -> object: ...


class UnsafeTarHeaderError(tarfile.TarError):
    pass


class BoundedTarInfo(tarfile.TarInfo):
    """확장 헤더를 제한하는 TarInfo.

    `_proc_member`는 CPython이 "하위 클래스에서 재정의할 수 있다"고 문서화한 진입점이지만
    typeshed에는 없어서, 부모 구현은 `getattr`로 꺼내 호출 형태를 명시한다.
    """

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.type in _FORBIDDEN:
            raise UnsafeTarHeaderError(f"tar header type {self.type!r} is not allowed")
        if self.type not in _EXTENDED:
            return self._base_proc_member(archive)
        if self.size > EXTENDED_HEADER_LIMIT:
            raise UnsafeTarHeaderError(
                f"extended header of {self.size} bytes exceeds {EXTENDED_HEADER_LIMIT}"
            )
        if _after_extended.get():
            raise UnsafeTarHeaderError("chained extended headers are not allowed")
        token = _after_extended.set(True)
        try:
            return self._base_proc_member(archive)
        except (ValueError, UnicodeError) as exc:
            # pax 값 해석(int() 등)의 원시 예외가 TarFile.next() 밖으로 새지 않게 한다.
            raise UnsafeTarHeaderError(f"invalid extended header ({exc})") from exc
        finally:
            _after_extended.reset(token)

    def _base_proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        parent = cast(Callable[[tarfile.TarFile], tarfile.TarInfo], getattr(super(), _PARENT_HOOK))
        return parent(archive)

    # GNU sparse(pax 0.0·0.1·1.0)는 맵을 데이터에서 끝까지 숫자로 읽어(CPU·메모리 고갈) 처리 전에
    # 거부한다. CPython `_proc_pax`가 sparse 판정 시 아래 훅을 부르고, 그다음 `_apply_pax_info`를
    # 부른다(realsize 단독 등). 두 경로 모두 막는다.
    def _proc_gnusparse_00(self, member: tarfile.TarInfo, raw_headers: object) -> None:
        raise UnsafeTarHeaderError("GNU sparse (pax 0.0) is not allowed")

    def _proc_gnusparse_01(self, member: tarfile.TarInfo, pax_headers: object) -> None:
        raise UnsafeTarHeaderError("GNU sparse (pax 0.1) is not allowed")

    def _proc_gnusparse_10(
        self, member: tarfile.TarInfo, pax_headers: object, archive: tarfile.TarFile
    ) -> None:
        raise UnsafeTarHeaderError("GNU sparse (pax 1.0) is not allowed")

    def _apply_pax_info(self, pax_headers: Mapping[str, str], encoding: str, errors: str) -> None:
        sparse = sorted(key for key in pax_headers if key.startswith(_SPARSE_PREFIX))
        if sparse:
            raise UnsafeTarHeaderError(f"GNU sparse pax keys are not allowed: {sparse}")
        parent = cast(Callable[[Mapping[str, str], str, str], None], getattr(super(), _APPLY_HOOK))
        parent(pax_headers, encoding, errors)


def open_tar_stream(stream: Readable | IO[bytes], mode: str = "r|") -> tarfile.TarFile:
    """앞에서부터 한 번만 읽는 tar 스트림(`r|` 비압축, `r|*` 압축 자동 판별)."""
    if mode == "r|*":
        return tarfile.open(fileobj=stream, mode="r|*", tarinfo=BoundedTarInfo)
    if mode == "r|":
        return tarfile.open(fileobj=stream, mode="r|", tarinfo=BoundedTarInfo)
    raise ValueError(f"unsupported stream mode {mode!r}")
