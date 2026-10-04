"""등록 전에도 쓰는 `pureiron-site` 명령: enroll·secret set·report export·설치 번들 검증."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from updater_fakes import Kit

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, open_document
from pureiron_updater import cli
from pureiron_updater.enroll import load_identity
from pureiron_updater.settings import Settings, merged_env, parse_env_file


class Host:
    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "pureiron"
        self.env_file = tmp / "etc" / "updater.env"
        self.kit = Kit(tmp)

    @property
    def state(self) -> Path:
        return self.root / "state"

    def env(self) -> dict[str, str]:
        return parse_env_file(self.env_file.read_text(encoding="utf-8"))

    def enrollment(self) -> dict[str, Json]:
        data = (self.state / "enrollment.json").read_bytes()
        return open_document(data, SitePurpose.ENROLLMENT)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    host = Host(tmp_path)
    monkeypatch.delenv("PUREIRON_SITE_ID", raising=False)
    monkeypatch.setenv("PUREIRON_ROOT", str(host.root))
    monkeypatch.setenv("PUREIRON_ROOT_PUB", str(host.kit.root_pub))
    monkeypatch.setenv("PUREIRON_ENV_FILE", str(host.env_file))
    return host


def test_env_file_fills_missing_variables(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.env_file.parent.mkdir(parents=True)
    host.env_file.write_text(
        '# comment\nPUREIRON_SITE_ID="site-a"\nPUREIRON_POLL_INTERVAL_S=5\n', encoding="utf-8"
    )
    monkeypatch.setenv("PUREIRON_POLL_INTERVAL_S", "7")
    settings = Settings.from_env(merged_env(os.environ))
    assert settings.site_id == "site-a"
    assert settings.poll_interval_s == 7.0  # 프로세스 환경이 우선


def test_enroll_creates_key_identity_and_signed_enrollment(host: Host) -> None:
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK
    key = host.state / "site.key"
    if sys.platform != "win32":
        assert key.stat().st_mode & 0o777 == 0o600
    identity = load_identity(host.state)
    assert identity is not None
    assert (identity.site_id, identity.next_report_seq) == ("site-a", 1)
    payload = host.enrollment()
    assert payload["public_key"] == keys.public_key(key.read_bytes()).hex()
    assert payload["fingerprint"] == identity.fingerprint
    assert host.env()["PUREIRON_SITE_ID"] == "site-a"
    # 플래그 없이 다시: 키는 그대로, enrollment만 다시 쓴다.
    seed = key.read_bytes()
    out = host.root / "usb" / "enroll.json"
    out.parent.mkdir(parents=True)
    assert cli.main(["enroll", "--site", "site-a", "--out", str(out)]) == cli.EXIT_OK
    assert key.read_bytes() == seed
    assert open_document(out.read_bytes(), SitePurpose.ENROLLMENT)["site_id"] == "site-a"


@pytest.mark.parametrize(
    "argv",
    [
        ["enroll", "--site", "Site_A"],
        ["enroll", "--site", "site-b"],
        ["enroll", "--site", "site-a", "--next-seq", "5"],
    ],
)
def test_enroll_refuses_inconsistent_requests(host: Host, argv: list[str]) -> None:
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK
    assert cli.main(argv) == cli.EXIT_CONFIG


def test_re_enroll_rotates_key_and_keeps_or_sets_seq(host: Host) -> None:
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK
    first = load_identity(host.state)
    assert cli.main(["enroll", "--site", "site-a", "--re-enroll"]) == cli.EXIT_OK
    second = load_identity(host.state)
    assert first is not None
    assert second is not None
    assert second.fingerprint != first.fingerprint
    assert second.next_report_seq == first.next_report_seq
    # 상태 분실: next_seq 없이 재등록하면 거부, 벤더 값을 주면 그 값부터.
    (host.state / "site.key").unlink()
    (host.state / "site.json").unlink()
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK  # 새 설치처럼 처음부터
    (host.state / "site.json").unlink()
    assert cli.main(["enroll", "--site", "site-a", "--re-enroll"]) == cli.EXIT_CONFIG
    assert cli.main(["enroll", "--site", "site-a", "--re-enroll", "--next-seq", "42"]) == 0
    third = load_identity(host.state)
    assert third is not None
    assert third.next_report_seq == 42
    assert host.enrollment()["next_report_seq"] == 42


def test_key_without_identity_needs_re_enroll(host: Host) -> None:
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK
    (host.state / "site.json").unlink()
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_CONFIG


def test_secret_set_from_stdin_and_generate(
    host: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"tok-123\r\n")))
    assert cli.main(["secret", "set", "control_api_token"]) == cli.EXIT_OK
    secrets = host.state / "secrets"
    assert (secrets / "control_api_token").read_bytes() == b"tok-123"
    if sys.platform != "win32":
        assert (secrets / "control_api_token").stat().st_mode & 0o777 == 0o600
    assert cli.main(["secret", "set", "db_password", "--generate", "--if-missing"]) == 0
    generated = (secrets / "db_password").read_bytes()
    assert len(generated) >= 40
    assert cli.main(["secret", "set", "db_password", "--generate", "--if-missing"]) == 0
    assert (secrets / "db_password").read_bytes() == generated
    assert "kept" in capsys.readouterr().out
    for bad in (b"\n", b"a\nb\n"):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(bad)))
        assert cli.main(["secret", "set", "x_token"]) == cli.EXIT_CONFIG
    assert cli.main(["secret", "set", "../evil", "--generate"]) == cli.EXIT_CONFIG


def _report(directory: Path, name: str, generation: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = {"generation": generation, "result": "succeeded", "verify_s": 1.25}
    (directory / name).write_text(json.dumps(body), encoding="utf-8")


def test_report_export_assigns_monotonic_seq_and_signs(host: Host) -> None:
    out = host.root / "usb"
    assert cli.main(["report", "export", "--out", str(out)]) == cli.EXIT_CONFIG  # 미등록
    assert cli.main(["enroll", "--site", "site-a"]) == cli.EXIT_OK
    reports = host.state / "reports"
    _report(reports, "20261005T010000000000Z-g1-aaaa.json", 1)
    _report(reports, "20261006T010000000000Z-g2-bbbb.json", 2)
    assert cli.main(["report", "export", "--out", str(out)]) == cli.EXIT_OK
    (bundle,) = out.glob("*.json")
    assert bundle.name == "pureiron-reports-site-a-000001-000002.json"
    public = keys.public_key((host.state / "site.key").read_bytes())
    payload = open_document(bundle.read_bytes(), SitePurpose.REPORTS, public)
    found = payload["reports"]
    assert isinstance(found, list)
    assert [(r["report_seq"], r["generation"]) for r in found if isinstance(r, dict)] == [
        (1, 1),
        (2, 2),
    ]
    assert not list(reports.glob("*.json"))
    assert len(list((reports / "exported").glob("*.json"))) == 2
    assert cli.main(["report", "export", "--out", str(out)]) == cli.EXIT_OK  # 대기 없음
    _report(reports, "20261007T010000000000Z-g3-cccc.json", 3)
    assert cli.main(["report", "export", "--out", str(out)]) == cli.EXIT_OK
    assert (out / "pureiron-reports-site-a-000003-000003.json").exists()


def test_install_bundle_verify_before_enroll(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    pib = host.kit.install_bundle()
    assert cli.main(["bundle", "verify", str(pib), "--kind", "install"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["kind"] == "install"
    # 현장 번들 검증은 등록(site id)이 필요하다.
    assert cli.main(["bundle", "verify", str(host.kit.bundle(1))]) == cli.EXIT_CONFIG
    data = bytearray(pib.read_bytes())
    data[data.index(b"# install/install.sh")] ^= 1
    pib.write_bytes(bytes(data))
    assert cli.main(["bundle", "verify", str(pib), "--kind", "install"]) == cli.EXIT_FAILED
