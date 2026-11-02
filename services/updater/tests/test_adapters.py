"""실제 어댑터: docker·compose 명령줄(fake runner), core HTTP·polling(로컬 HTTP 서버)."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from updater_fakes import FakeRunner, Kit, Site

from pureiron_updater.compose import ComposeCli
from pureiron_updater.core import CoreHttp
from pureiron_updater.images import DockerCli
from pureiron_updater.poll import ControlApi, PollError, SendError, poll_once
from pureiron_updater.ports import (
    CommandError,
    ComposeTarget,
    CoreUnavailableError,
    LockConflictError,
)
from pureiron_updater.proc import Result
from pureiron_updater.state import Phase


def test_compose_commands_pin_project_file_and_env(tmp_path: Path) -> None:
    runner = FakeRunner()
    compose = ComposeCli(runner)
    target = ComposeTarget(tmp_path / "g2")
    base = ["docker", "compose", "-p", "pureiron", "-f", str(target.compose_file)]
    base += ["--env-file", str(target.env_file)]
    compose.up(target)
    compose.up(target, ["site-db"])
    compose.run_core(
        target,
        ["migrate", "up", "--to", "2"],
        mounts={tmp_path / "p.json": "/x"},
        writable={tmp_path / "out": "/o"},
    )
    compose.dump_db(target, tmp_path / "db.dump")
    compose.down(target)
    compose.release_lock_db(target)
    wait = ["up", "-d", "--wait", "--wait-timeout", "180"]
    assert runner.calls[0] == [*base, *wait, "--remove-orphans"]
    assert runner.calls[1] == [*base, *wait, "site-db"]
    assert runner.calls[2] == [
        *base,
        *("run", "--rm", "--no-deps", "-T", "--volume", f"{tmp_path / 'p.json'}:/x:ro"),
        *("--volume", f"{tmp_path / 'out'}:/o"),
        *("core", "migrate", "up", "--to", "2"),
    ]
    dump = runner.calls[3]
    assert dump[len(base) :][:4] == ["exec", "-T", "site-db", "pg_dump"]
    assert "--exclude-table-data=tag_history" in dump
    assert "--exclude-table-data=_timescaledb_internal.*" in dump
    assert (tmp_path / "db.dump").read_bytes() == b"dump"
    assert runner.calls[4] == [*base, "down"]
    unlock = runner.calls[5]
    assert unlock[len(base) :][:4] == ["exec", "-T", "site-db", "psql"]
    assert "WHERE id AND edit_lock_owner = 'updater'" in unlock[-1]


def test_compose_reads_config_and_running_images(tmp_path: Path) -> None:
    config = {"services": {"core": {"image": "pureiron/core:r1"}, "web": {"image": "w:r1"}}}
    runner = FakeRunner(
        [
            Result(0, json.dumps(config), ""),
            Result(0, "c1\nc2\n", ""),
            Result(0, "core sha256:aa\nweb sha256:bb\n", ""),
        ]
    )
    compose = ComposeCli(runner)
    target = ComposeTarget(tmp_path)
    assert compose.config_images(target) == {"core": "pureiron/core:r1", "web": "w:r1"}
    assert compose.running_images(target) == {"core": "sha256:aa", "web": "sha256:bb"}
    assert runner.calls[2][:3] == ["docker", "inspect", "--format"]


def test_command_failure_raises_with_stderr_tail(tmp_path: Path) -> None:
    compose = ComposeCli(FakeRunner([Result(1, "", "boom\nmigration 0042 failed")]))
    with pytest.raises(CommandError, match="migration 0042 failed"):
        compose.run_core(ComposeTarget(tmp_path), ["migrate", "up"])


def test_docker_image_id_missing_is_none() -> None:
    runner = FakeRunner(
        [Result(1, "", "Error: No such image: x"), Result(0, "sha256:ab\n", ""), Result(1, "", "?")]
    )
    docker = DockerCli(runner)
    assert docker.image_id("x") is None
    assert docker.image_id("pureiron/core:r0") == "sha256:ab"
    with pytest.raises(CommandError):
        docker.image_id("y")
    docker.load(Path("core.tar.gz"))
    assert runner.calls[-1] == ["docker", "load", "--input", "core.tar.gz"]
    assert docker.available()
    assert not DockerCli(FakeRunner([Result(1, "", "Cannot connect")])).available()


# ── 로컬 HTTP 서버 ────────────────────────────────────────────────────────


@dataclass
class Server:
    routes: dict[tuple[str, str], tuple[int, bytes]] = field(
        default_factory=dict[tuple[str, str], tuple[int, bytes]]
    )
    requests: list[tuple[str, str, dict[str, str], bytes]] = field(
        default_factory=list[tuple[str, str, dict[str, str], bytes]]
    )
    blob: bytes = b""
    cut_after: int | None = None
    """다운로드 응답을 이 바이트 수에서 끊는다(연결 끊김 흉내, 한 번만)."""
    url: str = ""


def _handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            server.requests.append((self.command, self.path, headers, body))
            if self.path == "/bundle.pib":
                self._bundle(headers)
                return
            status, payload = server.routes.get((self.command, self.path), (404, b"{}"))
            self.send_response(status)
            if self.path.endswith("/desired") and status == 200:
                self.send_header("ETag", '"g"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _bundle(self, headers: dict[str, str]) -> None:
            start = 0
            if "range" in headers:
                start = int(headers["range"].removeprefix("bytes=").rstrip("-"))
            data = server.blob[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if server.cut_after is not None:
                self.wfile.write(data[: server.cut_after])
                server.cut_after = None
                self.close_connection = True
                return
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def do_DELETE(self) -> None:
            self._serve()

    return Handler


@pytest.fixture
def server() -> Iterator[Server]:
    state = Server()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_core_http_lock_project_readiness(tmp_path: Path, server: Server) -> None:
    token = tmp_path / "updater_token"
    token.write_text("t0k\n", encoding="utf-8")
    server.routes = {
        ("POST", "/api/v1/project/edit-lock"): (200, b'{"site_rev": 4}'),
        ("DELETE", "/api/v1/project/edit-lock?owner=updater"): (204, b""),
        ("GET", "/api/v1/project"): (200, b'{"site_rev": 4, "base_site_rev": 3}'),
        (
            "GET",
            "/api/v1/readyz",
        ): (503, b'{"status": "not_ready", "checks": {"collector": {"ok": true}}}'),
    }
    core = CoreHttp(server.url + "/api/v1", token)
    assert core.acquire_lock(7) == 4
    method, path, headers, body = server.requests[-1]
    assert (method, path) == ("POST", "/api/v1/project/edit-lock")
    assert headers["authorization"] == "Bearer t0k"
    assert json.loads(body) == {"owner": "updater", "generation": 7}
    core.release_lock()
    assert core.project() == (4, 3)
    ready = core.readiness()
    assert (ready.ready, ready.collector, ready.good_ratio) == (False, True, None)
    # core r2+: /readyz acquisition 집계의 GOOD 비율(r1 core처럼 없으면 None)
    for acquisition, expected in [
        (b'{"good_ratio": 0.75}', 0.75),
        (b'{"good_ratio": 1}', 1.0),
        (b'{"good_ratio": null}', None),
        (b'{"good_ratio": true}', None),
        (b'{"good_ratio": 1.5}', None),
        (b"[]", None),
    ]:
        server.routes[("GET", "/api/v1/readyz")] = (
            200,
            b'{"status": "ready", "checks": {"collector": {"ok": true}}, "acquisition": '
            + acquisition
            + b"}",
        )
        ready = core.readiness()
        assert (ready.ready, ready.good_ratio) == (True, expected), acquisition
    server.routes[("POST", "/api/v1/project/edit-lock")] = (409, b'{"detail": "held"}')
    with pytest.raises(LockConflictError):
        core.acquire_lock(8)
    with pytest.raises(CoreUnavailableError):
        CoreHttp("http://127.0.0.1:9/api/v1", token).project()


def _desired(server: Server, generation: int, blob: bytes) -> None:
    server.blob = blob
    body = {
        "generation": generation,
        "bundle_url": server.url + "/bundle.pib",
        "sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
    }
    server.routes[("GET", "/sites/site-a/desired")] = (200, json.dumps(body).encode())


def test_poll_downloads_with_range_resume_then_applies(tmp_path: Path, server: Server) -> None:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    token = site.settings.secrets_dir / "control_api_token"
    token.parent.mkdir(parents=True)
    token.write_text("ctl", encoding="utf-8")
    api = ControlApi(server.url, "site-a", token)
    blob = site.kit.bundle(1, "r0").read_bytes()
    _desired(server, 1, blob)
    server.cut_after = 1000
    updater = site.updater()
    with pytest.raises(PollError):
        poll_once(updater, api)
    sha = hashlib.sha256(blob).hexdigest()
    part = site.settings.bundles_dir / f"poll-{sha[:16]}.pib.part"
    assert part.stat().st_size == 1000
    assert poll_once(updater, api)
    ranges = [h.get("range") for _, p, h, _ in server.requests if p == "/bundle.pib"]
    assert ranges == [None, "bytes=1000-"]
    assert server.requests[0][2]["authorization"] == "Bearer ctl"
    attempt = updater.state.attempt
    assert attempt is not None
    assert attempt.phase is Phase.FETCHED
    state = updater.advance()
    assert state.applied_gen == 1
    # 이미 적용한 generation은 다시 받지 않는다.
    assert not poll_once(updater, api)


def test_poll_discards_corrupt_download(tmp_path: Path, server: Server) -> None:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    token = site.settings.secrets_dir / "control_api_token"
    token.parent.mkdir(parents=True)
    token.write_text("ctl", encoding="utf-8")
    api = ControlApi(server.url, "site-a", token)
    blob = site.kit.bundle(1, "r0").read_bytes()
    _desired(server, 1, blob)
    server.blob = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(PollError, match="sha256"):
        poll_once(site.updater(), api)
    assert not list(site.settings.bundles_dir.glob("*.part"))
    assert site.updater().state.attempt is None


def test_poll_sends_token_only_to_control_api_origin(tmp_path: Path, server: Server) -> None:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    token = site.settings.secrets_dir / "control_api_token"
    token.parent.mkdir(parents=True)
    token.write_text("ctl", encoding="utf-8")
    blob = site.kit.bundle(1, "r0").read_bytes()
    _desired(server, 1, blob)
    # 같은 서버지만 호스트 이름이 다르면 다른 출처다(CDN·사전 서명 URL 흉내).
    desired = json.loads(server.routes[("GET", "/sites/site-a/desired")][1])
    desired["bundle_url"] = desired["bundle_url"].replace("127.0.0.1", "localhost")
    server.routes[("GET", "/sites/site-a/desired")] = (200, json.dumps(desired).encode())
    assert poll_once(site.updater(), ControlApi(server.url, "site-a", token))
    by_path = {p: h for _, p, h, _ in server.requests}
    assert by_path["/sites/site-a/desired"]["authorization"] == "Bearer ctl"
    assert "authorization" not in by_path["/bundle.pib"]


def test_control_api_post_returns_status_and_body(tmp_path: Path, server: Server) -> None:
    """송신 `POST`: 같은 출처 token·Content-Type, 4xx는 응답, 닿지 못하면 `SendError`."""
    token = tmp_path / "control_api_token"
    token.write_text("ctl\n", encoding="utf-8")
    api = ControlApi(server.url, "site-a", token)
    server.routes[("POST", "/sites/site-a/heartbeat")] = (200, b'{"outcome": "applied"}')
    server.routes[("POST", "/sites/site-a/reports")] = (400, b'{"detail": "bad"}')
    assert api.post(api.site_path("heartbeat"), b"{}", "application/json") == (
        200,
        b'{"outcome": "applied"}',
    )
    assert api.post(api.site_path("reports"), b"[]", "application/json") == (
        400,
        b'{"detail": "bad"}',
    )
    method, path, headers, body = server.requests[0]
    assert (method, path, body) == ("POST", "/sites/site-a/heartbeat", b"{}")
    assert headers["authorization"] == "Bearer ctl"
    assert headers["content-type"] == "application/json"
    token.unlink()
    with pytest.raises(SendError, match="control_api_token"):
        api.post(api.site_path("heartbeat"), b"{}", "application/json")
    closed = ControlApi("http://127.0.0.1:9", "site-a", tmp_path / "missing")
    with pytest.raises(SendError):
        closed.post("sites/site-a/heartbeat", b"{}", "application/json")
