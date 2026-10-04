from __future__ import annotations

from pathlib import Path

from normalize_venv import normalize


def test_removes_uv_cache_and_its_record_line(tmp_path: Path) -> None:
    dist = tmp_path / "lib" / "python3.12" / "site-packages" / "pkg-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "uv_cache.json").write_text('{"timestamp": 1}', encoding="utf-8")
    (dist / "RECORD").write_text(
        "pkg/__init__.py,sha256=abc,10\n"
        "pkg-0.1.0.dist-info/uv_cache.json,sha256=def,16\n"
        "pkg-0.1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    removed = normalize(tmp_path)
    assert removed == [dist / "uv_cache.json"]
    assert not (dist / "uv_cache.json").exists()
    assert (dist / "RECORD").read_text(encoding="utf-8") == (
        "pkg/__init__.py,sha256=abc,10\npkg-0.1.0.dist-info/RECORD,,\n"
    )
    assert normalize(tmp_path) == []
