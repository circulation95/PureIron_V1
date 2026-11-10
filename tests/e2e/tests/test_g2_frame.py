"""G2 pytest 틀(E7·E8) — 실패 분류는 예외 종류로 갈리고, site-b는 NEXT를 기대하며, G2 시험은
gates/pytest.ini로만 수집된다(G1 `unit`의 루트 설정에서는 수집 자체가 빠진다)."""

from __future__ import annotations

import subprocess
import sys

import pytest
from harness import buildhost, failures, g2, paths
from harness.context import E2EError, InfraError
from harness.golden import WslError
from harness.hyperv import HyperVError
from harness.release_pair import nightly
from harness.vm import VmError

COLLECT_TIMEOUT_S = 120
SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"


@pytest.mark.parametrize(
    "exc",
    [
        InfraError("preflight: 2.1 GB free"),
        VmError("10.77.0.11 not reachable over SSH in 900s"),
        HyperVError("powershell failed (1)"),
        WslError("wsl failed (1): 0x8007274c"),
        subprocess.TimeoutExpired("ssh", 120),
    ],
)
def test_environment_failures_are_infra(exc: BaseException) -> None:
    """변이 (ii): infra를 product로 분류하면 nightly가 다시 돌리지 않고 제품 이슈를 연다."""
    assert failures.classify(exc) == "infra"


@pytest.mark.parametrize(
    "exc",
    [
        AssertionError("site-b: ['tags_good']"),
        E2EError("install failed"),
        buildhost.HeadBuildError("HEAD 1a2b3c4 build failed"),
        KeyError("images"),
    ],
)
def test_product_failures_are_product(exc: BaseException) -> None:
    assert failures.classify(exc) == "product"


def test_site_a_installs_base_and_site_b_installs_next() -> None:
    """E2·변이 (v): site-b의 기대 digest가 BASE가 되면 HEAD 이미지가 한 번도 실행되지 않는다."""
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, 5)
    assert g2.site_release(pair, "site-a") == "r5"
    assert g2.site_release(pair, "site-b") == "n1a2b3c4"
    assert [s.site_id for s, _ in g2.SITE_ROLES] == ["site-a", "site-b"]


def test_results_live_under_the_e2e_root() -> None:
    assert g2.results_dir("20261110T180000Z") == paths.E2E / "results" / "g2-20261110T180000Z"


def collect(*args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=paths.REPO,
        capture_output=True,
        text=True,
        timeout=COLLECT_TIMEOUT_S,
        check=False,
    )
    return done.stdout


def test_g2_tests_are_collected_only_with_the_gates_config() -> None:
    gates = "tests/e2e/gates"
    g2_run = collect("-c", f"{gates}/pytest.ini", gates, "-m", "g2")
    assert "test_g2_1_install_and_100_tags_good[site-a]" in g2_run
    assert "test_g2_1_install_and_100_tags_good[site-b]" in g2_run
    # G1 `unit`과 같은 루트 설정(pyproject.toml)으로 gates 디렉터리를 줘도 G2 파일은 수집되지 않는다
    root_run = collect("-c", "pyproject.toml", "-p", "no:cacheprovider", gates)
    assert "test_g2_install" not in root_run
    assert "no tests collected" in root_run
