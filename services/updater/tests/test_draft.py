"""`pureiron-site project export`(M5-2 P6): 적용된 generation의 core로 `export --draft` → site key
서명 `draft.tar`. 등록 전·적용 전·core 실패·컨테이너 출력 위반은 파일 없이 거부, 같은 상태·시각은
같은 바이트, CLI 종료 코드와 드래프트 없음 안내."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from updater_fakes import T0, Kit, Site

from pureiron_bundle import keys
from pureiron_bundle.sitesign import open_draft
from pureiron_updater import cli
from pureiron_updater.draft import WORK_DIR, DraftError, export_draft
from pureiron_updater.enroll import EnrollError, enroll
from pureiron_updater.phases import Deps
from pureiron_updater.settings import Settings


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))


def _settings(site: Site) -> Settings:
    return replace(site.settings, env_file=site.root.parent / "etc" / "updater.env")


def _enrolled(site: Site) -> bytes:
    """등록하고 site 공개키를 돌려준다."""
    settings = _settings(site)
    enroll(settings, "site-a", now=T0)
    return keys.public_key(keys.read_private_key(settings.site_key_file))


def _installed(site: Site) -> Site:
    """g1 = r4(트리를 아는 core) 최초 설치 → 현장 PATCH 3회(site_rev 3)."""
    assert site.apply(site.kit.bundle(1, "r4")).applied_gen == 1
    site.world.site_rev = 3
    return site


def _ready(site: Site) -> bytes:
    """설치 순서대로 등록(install.sh) → 최초 설치 → PATCH 3회. site 공개키."""
    public = _enrolled(site)
    _installed(site)
    return public


def test_export_signs_effective_tree_with_site_key(site: Site, tmp_path: Path) -> None:
    public = _ready(site)
    out = tmp_path / "usb" / "draft.tar"
    result = export_draft(_settings(site), site.deps().compose, out, now=T0)
    payload, tree = open_draft(out.read_bytes(), public, site_id="site-a")
    assert payload == {
        "site_id": "site-a",
        "site_rev": 3,
        "base_site_rev": 0,
        "content_hash": site.world.project["content_hash"],
        "exported_at": "2026-10-05T01:00:00Z",
        "project_tar_sha256": "sha256:" + hashlib.sha256(b"project-v1").hexdigest(),
    }
    assert tree == site.world.tree == b"project-v1"
    assert (result.payload, result.has_drafts) == (payload, True)
    assert not (_settings(site).state_dir / WORK_DIR).exists()

    again = tmp_path / "again.tar"
    export_draft(_settings(site), site.deps().compose, again, now=T0)
    assert again.read_bytes() == out.read_bytes()  # 같은 상태·시각 = 같은 바이트


def test_without_drafts_file_is_still_written(site: Site, tmp_path: Path) -> None:
    _ready(site)
    site.world.site_rev = site.world.base_site_rev
    result = export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)
    assert result.has_drafts is False
    assert result.path.is_file()


def test_not_enrolled_is_rejected_before_core(site: Site, tmp_path: Path) -> None:
    _installed(site)
    ops = site.world.ops
    with pytest.raises(EnrollError, match="not enrolled"):
        export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)
    assert site.world.ops == ops
    assert not (tmp_path / "d.tar").exists()


def test_no_applied_generation_is_rejected(site: Site, tmp_path: Path) -> None:
    _enrolled(site)
    with pytest.raises(DraftError, match="no applied generation"):
        export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)


def test_core_failure_leaves_no_file(site: Site, tmp_path: Path) -> None:
    _ready(site)
    site.world.tree = None  # project_files가 빈 현장(트리 apply 전)
    with pytest.raises(DraftError, match="project_files is empty"):
        export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)
    assert not (tmp_path / "d.tar").exists()
    assert not (_settings(site).state_dir / WORK_DIR).exists()


def test_unexpected_container_output_is_rejected(site: Site, tmp_path: Path) -> None:
    _ready(site)
    site.world.draft_extra = {"evil.txt": b"x"}
    with pytest.raises(DraftError, match=r"evil\.txt"):
        export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)
    assert not (tmp_path / "d.tar").exists()


def test_unremovable_work_dir_is_draft_error_with_path(site: Site, tmp_path: Path) -> None:
    """이전 실행이 남긴 작업 경로를 지울 수 없으면(여기선 디렉터리가 아닌 파일) 설정 오류가 아니라
    `DraftError`(exit 1)이고 메시지에 경로가 있다. core는 부르지 않는다."""
    _ready(site)
    work = _settings(site).state_dir / WORK_DIR
    work.write_bytes(b"stale")
    ops = site.world.ops
    with pytest.raises(DraftError, match=re.escape(str(work))):
        export_draft(_settings(site), site.deps().compose, tmp_path / "d.tar", now=T0)
    assert site.world.ops == ops
    assert not (tmp_path / "d.tar").exists()


@pytest.fixture
def cli_site(site: Site, monkeypatch: pytest.MonkeyPatch) -> Site:
    monkeypatch.delenv("PUREIRON_SITE_ID", raising=False)
    monkeypatch.setenv("PUREIRON_ROOT", str(site.root))
    monkeypatch.setenv("PUREIRON_ROOT_PUB", str(site.kit.root_pub))
    monkeypatch.setenv("PUREIRON_ENV_FILE", str(_settings(site).env_file))

    def fake_deps(_settings: Settings) -> Deps:
        return site.deps()

    monkeypatch.setattr(cli, "real_deps", fake_deps)
    return site


def test_cli_not_enrolled_is_config_error(
    cli_site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed(cli_site)
    out = tmp_path / "draft.tar"
    assert cli.main(["project", "export", "--out", str(out)]) == cli.EXIT_CONFIG
    assert "not enrolled" in capsys.readouterr().err
    assert not out.exists()


def test_cli_project_export(
    cli_site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    public = _ready(cli_site)
    out = tmp_path / "draft.tar"
    assert cli.main(["project", "export", "--out", str(out)]) == cli.EXIT_OK
    assert "site_rev:      3 (base 0)" in capsys.readouterr().out
    assert open_draft(out.read_bytes(), public, site_id="site-a")[0]["site_rev"] == 3

    cli_site.world.site_rev = 0
    assert cli.main(["project", "export", "--out", str(out)]) == cli.EXIT_OK
    assert "no site drafts" in capsys.readouterr().out

    cli_site.world.tree = None
    assert cli.main(["project", "export", "--out", str(out)]) == cli.EXIT_FAILED


@dataclass
class DraftSink:
    """`POST /sites/{site}/drafts`만 받는 로컬 control-api(상태는 `status`)."""

    url: str = ""
    status: int = 201
    received: list[tuple[str, dict[str, str], bytes]] = field(
        default_factory=list[tuple[str, dict[str, str], bytes]]
    )


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch, cli_site: Site) -> Iterator[DraftSink]:
    state = DraftSink()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.received.append((self.path, headers, body))
            self.send_response(state.status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    token = _settings(cli_site).secrets_dir / "control_api_token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("pis_x\n", encoding="utf-8")
    monkeypatch.setenv("PUREIRON_CONTROL_API", state.url)
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_cli_project_export_upload(
    cli_site: Site, sink: DraftSink, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """E8: 파일을 만든 뒤 `POST /sites/{site}/drafts`. 실패하면 exit 1이고 파일은 남는다."""
    _ready(cli_site)
    out = tmp_path / "draft.tar"
    assert cli.main(["project", "export", "--out", str(out), "--upload"]) == cli.EXIT_OK
    assert "uploaded:      HTTP 201" in capsys.readouterr().out
    path, headers, body = sink.received[-1]
    assert path == "/api/v1/sites/site-a/drafts"
    assert headers["authorization"] == "Bearer pis_x"
    assert headers["content-type"] == "application/x-tar"
    assert body == out.read_bytes()

    sink.status = 403
    out.unlink()
    assert cli.main(["project", "export", "--out", str(out), "--upload"]) == cli.EXIT_FAILED
    assert "upload refused: HTTP 403" in capsys.readouterr().err
    assert out.exists()

    # 리뷰 M1: 등록 키 불일치(409 — re-enroll 뒤 rekey 대기)도 실패(exit 1) + 파일 유지 + 안내.
    sink.status = 409
    out.unlink()
    assert cli.main(["project", "export", "--out", str(out), "--upload"]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "upload refused: HTTP 409" in err
    assert "rekey" in err
    assert out.exists()


def test_cli_upload_without_control_api_is_refused_before_export(
    cli_site: Site,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUREIRON_CONTROL_API", raising=False)
    _ready(cli_site)
    ops = cli_site.world.ops
    out = tmp_path / "draft.tar"
    assert cli.main(["project", "export", "--out", str(out), "--upload"]) == cli.EXIT_CONFIG
    assert "PUREIRON_CONTROL_API" in capsys.readouterr().err
    assert cli_site.world.ops == ops
    assert not out.exists()


def test_cli_upload_unreachable_keeps_the_file(
    cli_site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(cli_site)
    monkeypatch.setenv("PUREIRON_CONTROL_API", "http://127.0.0.1:9/api/v1")
    out = tmp_path / "draft.tar"
    assert cli.main(["project", "export", "--out", str(out), "--upload"]) == cli.EXIT_FAILED
    assert out.exists()
