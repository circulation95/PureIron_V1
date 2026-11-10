"""nightly release 쌍(D8·E4) — BASE는 태그가 있는 최신 r*, NEXT는 HEAD 빌드 `n<sha7>`이고 그 버전은
fleet 계약 패턴을 통과하며, HEAD 빌드 실패는 제품 실패(`HeadBuildError`)다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import buildhost, bundles, paths
from harness.context import E2EError
from harness.release_pair import DEFAULT, latest_tagged, nightly, nightly_version

from pureiron_bundle.manifest import release_version_pattern

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"


def repo_records() -> dict[str, dict[str, object]]:
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((paths.REPO / "deploy" / "releases").glob("r*.json"))
    }


def test_base_is_the_latest_tagged_release() -> None:
    empty: dict[str, object] = {}
    records: dict[str, dict[str, object]] = dict.fromkeys(
        ("r0", "r3", "r4", "r5", "r5f", "c0"), empty
    )
    assert latest_tagged(records, {"r0", "r4", "r5"}) == "r5"
    assert latest_tagged(records, {"r0", "r4"}) == "r4"
    assert latest_tagged({"r10": empty, "r9": empty}, {"r9", "r10"}) == "r10"
    with pytest.raises(E2EError, match="no tagged"):
        latest_tagged(records, {"r5f", "c0", "r7"})


def test_nightly_version_bumps_the_last_number_and_passes_the_contract() -> None:
    """착수 확인: fleet-site `$defs/release`는 `-<접미사>`를 받는다(`2026.11.1-fault1` 선례)."""
    assert nightly_version("2026.11.1", SHA) == "2026.11.2-nightly.1a2b3c4"
    assert nightly_version("2026.9.9", SHA) == "2026.9.10-nightly.1a2b3c4"
    assert release_version_pattern().fullmatch(nightly_version("2026.11.1", SHA))
    assert release_version_pattern().fullmatch("2026.11.1-fault1")
    with pytest.raises(E2EError, match=r"YYYY\.M\.N"):
        nightly_version("2026.11.1-fault1", SHA)


def test_nightly_pair_from_the_repository_records() -> None:
    pair = nightly(repo_records(), {"r0", "r4", "r5"}, SHA, 6)
    assert (pair.base, pair.next, pair.fault) == ("r5", "n1a2b3c4", "")
    assert dict(pair.releases) == {"r5": "2026.11.1", "n1a2b3c4": "2026.11.2-nightly.1a2b3c4"}
    assert dict(pair.epochs) == {"r5": 5, "n1a2b3c4": 6}
    # E5: G2-1은 스키마·트리 판정을 하지 않는다
    assert not (pair.caggs | pair.base_tables | pair.base_columns | pair.base_indexes)
    assert not (pair.next_tables | pair.next_indexes)
    assert DEFAULT.base == "r4"  # 손으로 쓴 쌍(run.py)은 그대로


def test_nightly_pair_needs_a_full_head_sha() -> None:
    with pytest.raises(E2EError, match="40 lowercase hex"):
        nightly(repo_records(), {"r5"}, SHA[:7], 5)


def test_nightly_label_makes_the_app_image_differ_from_base() -> None:
    dockerfile = bundles.NIGHTLY_DOCKERFILE.format(svc="core", source="n1a2b3c4-src", sha=SHA)
    assert dockerfile.splitlines() == [
        "FROM pureiron/core:n1a2b3c4-src",
        f"LABEL pureiron.nightly={SHA}",
    ]
    assert bundles.NIGHTLY_BUILD == ("core", "collector", "web", "simulator")


def test_nightly_prune_keeps_the_current_and_the_newest() -> None:
    names = {"n0000001": 1.0, "n0000002": 2.0, "n0000003": 3.0, "n0000004": 4.0, "ncur000": 0.5}
    assert bundles.nightly_prune(names, "ncur000") == ["n0000001", "n0000002"]
    assert bundles.nightly_prune(names, "ncur000", keep=1) == [
        "n0000001",
        "n0000002",
        "n0000003",
        "n0000004",
    ]
    assert bundles.nightly_prune({"ncur000": 1.0}, "ncur000") == []


class FakeWsl:
    def __init__(self, reply: dict[str, object]) -> None:
        self.reply = reply
        self.scripts: list[str] = []

    def __call__(self, script: str, *, timeout_s: float) -> str:
        del timeout_s
        self.scripts.append(script)
        return "build log line\n" + json.dumps(self.reply) + "\n"


def touch(path: Path) -> None:
    path.write_bytes(b"bundle")


def test_nightly_release_returns_the_next_record(tmp_path: Path) -> None:
    pair = nightly(repo_records(), {"r5"}, SHA, 5)
    record: dict[str, object] = {"name": "n1a2b3c4", "version": pair.releases[pair.next]}
    fake = FakeWsl({"ok": True, "record": record})
    got = buildhost.nightly_release(tmp_path, pair, SHA, run_wsl=fake, make_bundle=touch)
    assert got == record
    script = fake.scripts[0]
    assert f"git -C {buildhost.NIGHTLY_SRC} checkout -q {SHA}" in script
    for flag in (
        f"--sha {SHA}",
        "--name n1a2b3c4",
        "--version 2026.11.2-nightly.1a2b3c4",
        "--base r5",
        "simulator.tar",
    ):
        assert flag in script
    assert not (tmp_path / "head.bundle").exists()


def test_head_build_failure_is_a_product_error(tmp_path: Path) -> None:
    """D7: HEAD 빌드 실패는 `E2EError`(제품)다 — `InfraError`가 아니다."""
    pair = nightly(repo_records(), {"r5"}, SHA, 5)
    fake = FakeWsl({"ok": False, "stage": "build", "rc": 1, "tail": "pip resolution failed"})
    with pytest.raises(buildhost.HeadBuildError, match="pip resolution failed") as raised:
        buildhost.nightly_release(tmp_path, pair, SHA, run_wsl=fake, make_bundle=touch)
    assert isinstance(raised.value, E2EError)
