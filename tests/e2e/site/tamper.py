"""번들 사본의 한 부분만 바꾸는 변조 도구(M6-3b G2-5). 현장 VM(설치 전 탐침)과 호스트(`E_DIGEST`
재료)가 같이 쓴다.

    python3 tamper.py <번들 사본> <멤버 glob>

`<멤버 glob>`(fnmatch)에 맞는 첫 일반 파일 멤버의 데이터 한가운데 바이트 하나를 그 자리에서
뒤집는다 — tar 헤더·크기·다른 멤버는 그대로라 서명된 메타데이터는 맞고 그 멤버의 sha256만 달라진다.
출력은 JSON 한 줄 `{"member", "offset", "size"}`. 맞는 멤버가 없으면 종료 코드 1. 원본이 아니라
사본에 쓴다(부르는 쪽 책임). 표준 라이브러리만 쓴다(현장 호스트의 /usr/bin/python3.12).
"""

from __future__ import annotations

import fnmatch
import json
import sys
import tarfile
from pathlib import Path


class TamperError(LookupError):
    pass


def locate(path: Path, pattern: str) -> tuple[str, int, int]:
    """(멤버 이름, 데이터 시작 오프셋, 크기) — `pattern`에 맞는 첫 일반 파일(크기 > 0)."""
    with tarfile.open(path, "r:") as tar:
        for member in tar:
            if member.isfile() and member.size > 0 and fnmatch.fnmatchcase(member.name, pattern):
                return member.name, member.offset_data, member.size
    raise TamperError(f"{path}: no non-empty member matches {pattern!r}")


def flip(path: Path, pattern: str) -> dict[str, object]:
    """`pattern`에 맞는 첫 멤버의 가운데 바이트를 뒤집는다(XOR 0xFF). 무엇을 바꿨는지 돌려준다."""
    name, start, size = locate(path, pattern)
    offset = start + size // 2
    with path.open("r+b") as f:
        f.seek(offset)
        old = f.read(1)
        f.seek(offset)
        f.write(bytes([old[0] ^ 0xFF]))
    return {"member": name, "offset": offset, "size": size}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: tamper.py <bundle copy> <member glob>", file=sys.stderr)
        return 2
    try:
        print(json.dumps(flip(Path(args[0]), args[1]), sort_keys=True), flush=True)
    except (TamperError, OSError, tarfile.TarError) as exc:
        print(f"tamper: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
