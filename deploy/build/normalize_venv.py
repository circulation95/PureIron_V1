"""이미지 빌드 단계에서 venv의 비결정적 설치 메타데이터를 지운다(재현 빌드, G1-7).

uv는 설치한 배포판의 `*.dist-info/uv_cache.json`에 설치 시각을 기록한다. uv 자체 캐시 무효화용이라
런타임에는 쓰이지 않으므로 지우고, 같은 dist-info의 RECORD에서 해당 줄을 뺀다.

    python3 normalize_venv.py /opt/pureiron/venv
"""

from __future__ import annotations

import sys
from pathlib import Path

VOLATILE = "uv_cache.json"


def normalize(venv: Path) -> list[Path]:
    removed: list[Path] = []
    for cache in sorted(venv.glob(f"lib/python*/site-packages/*.dist-info/{VOLATILE}")):
        record = cache.parent / "RECORD"
        if record.exists():
            suffix = f"{cache.parent.name}/{VOLATILE},"
            lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
            record.write_text(
                "".join(line for line in lines if not line.startswith(suffix)), encoding="utf-8"
            )
        cache.unlink()
        removed.append(cache)
    return removed


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    for path in normalize(Path(argv[0])):
        print(f"normalize_venv: removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
