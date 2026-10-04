"""`.pib` reader의 적대적 입력 처리(E_FORMAT)와 writer 입력 검사."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.pib import METADATA_LIMIT, open_pib, write_pib

META = [
    ("manifest.json", b"{}"),
    ("manifest.sig", b"s" * 64),
    ("release-cert.json", b"{}"),
    ("release-cert.sig", b"c" * 64),
]


def raw_tar(path: Path, members: Sequence[tarfile.TarInfo | tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for item in members:
            if isinstance(item, tarfile.TarInfo):
                tar.addfile(item)
            else:
                name, data = item
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return path


def special(name: str, kind: bytes, target: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


def read_all(path: Path) -> list[str]:
    with open_pib(path) as reader:
        reader.read_metadata()
        return [member.name for member in reader.iter_payload()]


def format_error(path: Path) -> str:
    with pytest.raises(BundleRejected) as info:
        read_all(path)
    assert info.value.code is RejectCode.E_FORMAT
    return info.value.detail


def test_reader_returns_metadata_then_sorted_payload(tmp_path: Path) -> None:
    path = raw_tar(tmp_path / "ok.pib", [*META, ("a/1", b"x"), ("b/2", b"y")])
    with open_pib(path) as reader:
        metadata = reader.read_metadata()
        assert list(metadata) == [name for name, _ in META]
        assert [(m.name, m.stream.read()) for m in reader.iter_payload()] == [
            ("a/1", b"x"),
            ("b/2", b"y"),
        ]


def test_optional_trust_update_sig_after_cert(tmp_path: Path) -> None:
    path = raw_tar(tmp_path / "ok.pib", [*META, ("trust-update.sig", b"t" * 64), ("a", b"")])
    with open_pib(path) as reader:
        assert "trust-update.sig" in reader.read_metadata()


@pytest.mark.parametrize(
    "name", ["../escape", "/etc/passwd", "a/../../b", "a\\b", "images/../x", ""]
)
def test_unsafe_paths_are_rejected(tmp_path: Path, name: str) -> None:
    detail = format_error(raw_tar(tmp_path / "x.pib", [*META, (name, b"x")]))
    assert "path" in detail


@pytest.mark.parametrize("name", ["a b", "a/./b", ".hidden/..", "x//y"])
def test_payload_outside_files_pattern_is_rejected(tmp_path: Path, name: str) -> None:
    format_error(raw_tar(tmp_path / "x.pib", [*META, (name, b"x")]))


@pytest.mark.parametrize(
    "member",
    [
        special("images/link", tarfile.SYMTYPE, "/etc/passwd"),
        special("images/hard", tarfile.LNKTYPE, "manifest.json"),
        special("images", tarfile.DIRTYPE),
        special("dev", tarfile.CHRTYPE),
        special("fifo", tarfile.FIFOTYPE),
    ],
)
def test_non_regular_members_are_rejected(tmp_path: Path, member: tarfile.TarInfo) -> None:
    assert "regular" in format_error(raw_tar(tmp_path / "x.pib", [*META, member]))


def test_symlink_metadata_is_rejected(tmp_path: Path) -> None:
    members: list[tarfile.TarInfo | tuple[str, bytes]] = [
        special("manifest.json", tarfile.SYMTYPE, "/etc/passwd"),
        *META[1:],
    ]
    format_error(raw_tar(tmp_path / "x.pib", members))


def test_duplicate_payload_is_rejected(tmp_path: Path) -> None:
    detail = format_error(raw_tar(tmp_path / "x.pib", [*META, ("a", b"1"), ("a", b"2")]))
    assert "duplicate" in detail


def test_duplicate_metadata_is_rejected(tmp_path: Path) -> None:
    assert "duplicate" in format_error(raw_tar(tmp_path / "x.pib", [*META, META[0]]))


@pytest.mark.parametrize(
    "order",
    [
        [META[1], META[0], META[2], META[3]],
        [META[0], META[1], META[3], META[2]],
        [META[0], ("trust-update.sig", b"t" * 64), META[1], META[2], META[3]],
    ],
)
def test_metadata_out_of_order_is_rejected(tmp_path: Path, order: list[tuple[str, bytes]]) -> None:
    assert "out of order" in format_error(raw_tar(tmp_path / "x.pib", list(order)))


def test_missing_metadata_is_rejected(tmp_path: Path) -> None:
    detail = format_error(raw_tar(tmp_path / "x.pib", [META[0], META[1], ("a", b"x")]))
    assert "missing" in detail


def test_metadata_after_payload_is_rejected(tmp_path: Path) -> None:
    members = [*META, ("a", b"x"), ("trust-update.sig", b"t" * 64)]
    assert "after payload" in format_error(raw_tar(tmp_path / "x.pib", list(members)))


def test_unsorted_payload_is_rejected(tmp_path: Path) -> None:
    detail = format_error(raw_tar(tmp_path / "x.pib", [*META, ("b", b"1"), ("a", b"2")]))
    assert "out of order" in detail


def test_oversized_metadata_is_rejected(tmp_path: Path) -> None:
    big = [("manifest.json", b" " * (METADATA_LIMIT + 1)), *META[1:]]
    assert "larger" in format_error(raw_tar(tmp_path / "x.pib", big))


def test_not_a_tar_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "x.pib"
    path.write_bytes(b"\x01" * 2048)
    format_error(path)


def test_truncated_payload_is_rejected(tmp_path: Path) -> None:
    path = raw_tar(tmp_path / "x.pib", [*META, ("a", b"x" * 100_000)])
    path.write_bytes(path.read_bytes()[:6000])

    def consume() -> None:
        with open_pib(path) as reader:
            reader.read_metadata()
            for member in reader.iter_payload():
                member.stream.read()

    with pytest.raises(BundleRejected) as info:
        consume()
    assert info.value.code is RejectCode.E_FORMAT


def test_writer_rejects_bad_inputs(tmp_path: Path) -> None:
    source = tmp_path / "f"
    source.write_bytes(b"x")
    metadata = dict(META)
    with pytest.raises(ValueError, match="missing"):
        write_pib(tmp_path / "o.pib", dict(META[:3]), {})
    with pytest.raises(ValueError, match="collides"):
        write_pib(tmp_path / "o.pib", metadata, {"manifest.json": source})
    with pytest.raises(ValueError, match="invalid payload"):
        write_pib(tmp_path / "o.pib", metadata, {"../x": source})
    assert not (tmp_path / "o.pib").exists()
