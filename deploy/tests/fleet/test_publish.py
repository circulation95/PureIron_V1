"""publish 흐름(F5): 가짜 GitHub·control-api·빌더 + 로컬 git 원격(실제 git).

승인 실패 시 빌드·PUT 0회, 이미 게시 건너뜀, 404 안내, 409, 현장 2개 중 1개 실패 시 나머지 진행 +
exit 1, 승인 사슬(직접 push → 사슬 끊김 → trust-base 복구), merge·squash·rebase 3방식, PR이 승인자
목록을 바꾼 경우. control-api 클라이언트는 가짜 HTTP 서버로 응답 처리(본문 전 409)를 본다.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fleet import publish
from fleet.approval import CHANGES_REQUESTED
from fleet.fleetfiles import SiteStatus
from fleet.publish import (
    BuildRequest,
    Chain,
    Config,
    ControlApi,
    ControlApiError,
    Deps,
    run,
    trust_base,
)

from .kit import (
    APPROVERS,
    FakeControl,
    FakeGitHub,
    NoTokens,
    Repo,
    approve,
    git,
    pull,
    site_yaml,
)

REPO = Path(__file__).resolve().parents[3]
ACME = REPO / "packages" / "project-tools" / "fixtures" / "acme"
OWNER = "acme"
FLEET = f"{OWNER}/fleet"


@dataclass(slots=True)
class FakeBuilder:
    requests: list[BuildRequest] = field(default_factory=list[BuildRequest])
    fail: dict[str, Exception] = field(default_factory=dict[str, Exception])

    def __call__(self, request: BuildRequest) -> Path:
        assert (request.project_dir / "project.yaml").is_file()  # 태그 checkout이 됐다
        self.requests.append(request)
        if request.site in self.fail:
            raise self.fail[request.site]
        request.out.mkdir(parents=True, exist_ok=True)
        pib = request.out / f"pureiron-{request.site}-g{request.generation}.pib"
        pib.write_bytes(b"pib:" + request.fleet_file.read_bytes())
        return pib


@dataclass(slots=True)
class World:
    tmp: Path
    fleet: Repo
    config: Config
    github: FakeGitHub
    control: FakeControl
    builder: FakeBuilder
    base: str

    @property
    def chain(self) -> Chain:
        return Chain.of(self.config)

    def deps(self) -> Deps:
        return Deps(self.github, self.control, NoTokens(), self.builder)

    def run(self, sha: str) -> int:
        return run(self.config, sha, self.deps())

    def open_pr(self, number: int, files: Mapping[str, str | None], *, commits: int = 1) -> str:
        """main에서 브랜치 pr-<n>을 만들어 커밋(들) → head sha. main으로 돌아온다."""
        self.fleet.branch(f"pr-{number}", "main")
        head = ""
        for i in range(commits):
            last = i == commits - 1
            head = self.fleet.commit(files if last else {f"notes/{number}-{i}.txt": "x"}, f"pr {i}")
        self.fleet.switch("main")
        return head

    def merge_pr(
        self, number: int, files: Mapping[str, str | None], *, reviewer: str = "circulation95"
    ) -> str:
        head = self.open_pr(number, files)
        merged = self.fleet.merge(f"pr-{number}")
        self.github.add(
            pull(number, merge_commit_sha=merged, head_sha=head), [approve(reviewer, head)], merged
        )
        return merged


@pytest.fixture
def world(tmp_path: Path) -> World:
    remotes = tmp_path / "remotes"
    fleet = Repo.create(remotes / OWNER / "fleet")
    base = fleet.commit(
        {
            "allowed_approvers.txt": APPROVERS,
            "sites/.retired/.gitkeep": "",
            "sites/site-a.yaml": site_yaml("site-a", 1),
        },
        "template",
    )
    project = Repo.create(remotes / OWNER / "pureiron-project-acme")
    shutil.copytree(ACME, project.path, dirs_exist_ok=True)
    git(project.path, "add", "-A")
    project.commit({}, "project")
    project.tag("v3")
    config = Config(
        fleet_repo=FLEET,
        git_base=str(remotes),
        state_dir=tmp_path / "state",
        bundles_dir=tmp_path / "bundles",
        metrics_dir=tmp_path / "metrics",
        keys_dir=tmp_path / "keys",
        ci_token_file=tmp_path / "ci_token",
        viewer_token_file=tmp_path / "viewer_token",
        app_key_file=tmp_path / "app.pem",
        app_id_file=tmp_path / "app_id",
    )
    Chain.of(config).record(base, "test trust-base")
    registered = SiteStatus(max_published_gen=1, current_product="2026.11.0")
    control = FakeControl(status={"site-a": registered, "site-b": SiteStatus(None, None)})
    return World(tmp_path, fleet, config, FakeGitHub(), control, FakeBuilder(), base)


TWO_SITES = {
    "sites/site-a.yaml": site_yaml("site-a", 2),
    "sites/site-b.yaml": site_yaml("site-b", 1),
}


def test_approved_merge_builds_and_publishes_each_site(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = world.merge_pr(7, TWO_SITES)
    assert world.run(merged) == 0
    assert world.control.puts == [
        ("site-a", 2, "pureiron-site-a-g2.pib"),
        ("site-b", 1, "pureiron-site-b-g1.pib"),
    ]
    request = world.builder.requests[0]
    assert request.fleet_commit == merged
    pib = request.out / "pureiron-site-a-g2.pib"  # 가짜 빌더가 fleet 파일 내용을 담는다
    assert pib.read_bytes() == b"pib:" + site_yaml("site-a", 2).encode()
    assert request.out == world.config.bundles_dir / "site-a"
    assert world.chain.last_approved() == merged
    metrics = json.loads((world.config.metrics_dir / "publish-site-a-g2.json").read_text("utf-8"))
    assert (metrics["fleet_commit"], metrics["pull"], metrics["approvers"]) == (
        merged,
        7,
        ["circulation95"],
    )
    out = capsys.readouterr().out
    assert "::notice::승인: PR #7, 승인자 circulation95" in out
    assert "게시 2, 건너뜀 0, 실패 0" in out


def test_approval_failure_builds_and_puts_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    head = world.open_pr(7, TWO_SITES)
    merged = world.fleet.merge("pr-7")
    world.github.add(
        pull(7, merge_commit_sha=merged, head_sha=head),
        [approve("circulation95", head), approve("circulation95", head, CHANGES_REQUESTED)],
        merged,
    )
    assert world.run(merged) == 1
    assert (world.builder.requests, world.control.puts, world.control.lookups) == ([], [], [])
    assert world.chain.last_approved() == world.base
    out = capsys.readouterr().out
    assert "::error::E_APPROVAL PR #7 유효 승인 0건" in out
    assert "서명하지 않는다" in out


def test_already_published_is_skipped_and_rerun_is_safe(world: World) -> None:
    merged = world.merge_pr(7, TWO_SITES)
    assert world.run(merged) == 0
    world.control.status["site-a"] = SiteStatus(2, "2026.11.0")
    world.control.status["site-b"] = SiteStatus(1, None)
    world.builder.requests.clear()
    world.control.puts.clear()
    assert world.run(merged) == 0  # 재실행: 사슬은 sha == last_approved로 통과, 둘 다 건너뜀
    assert (world.builder.requests, world.control.puts) == ([], [])


def test_unregistered_site_fails_with_guidance_others_continue(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.control.status["site-b"] = None
    merged = world.merge_pr(7, TWO_SITES)
    assert world.run(merged) == 1
    assert world.control.puts == [("site-a", 2, "pureiron-site-a-g2.pib")]
    out = capsys.readouterr().out
    assert "::error file=sites/site-b.yaml::E_PUBLISH 현장 'site-b'이 control-api에 미등록" in out
    assert "workflow_dispatch로 재실행" in out


def test_put_conflict_and_build_failure_are_reported_per_site(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.control.fail_put["site-a"] = ControlApiError("HTTP 409: generation 2 <= 2", 409)
    merged = world.merge_pr(7, TWO_SITES)
    assert world.run(merged) == 1
    assert world.control.puts == [("site-b", 1, "pureiron-site-b-g1.pib")]
    out = capsys.readouterr().out
    assert "게시 실패: HTTP 409: generation 2 <= 2 — 같은 gen이 이미 게시됐거나 gen 역행" in out
    world.control.fail_put.clear()
    world.builder.fail["site-a"] = ValueError("project invalid")
    world.control.status["site-b"] = SiteStatus(1, None)
    assert world.run(merged) == 1
    assert "빌드 실패: project invalid" in capsys.readouterr().out


def test_generation_behind_published_fails_without_build(world: World) -> None:
    world.control.status["site-a"] = SiteStatus(5, None)
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(merged) == 1
    assert world.builder.requests == []


def test_direct_push_breaks_chain_until_trust_base(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    pushed = world.fleet.commit({"sites/site-a.yaml": site_yaml("site-a", 2)}, "direct push")
    assert world.run(pushed) == 1
    assert "직접 push" in capsys.readouterr().out
    merged = world.merge_pr(8, {"sites/site-b.yaml": site_yaml("site-b", 1)})
    assert world.run(merged) == 1
    assert "승인 사슬 끊김" in capsys.readouterr().out
    assert world.builder.requests == []
    # 사람이 직접 push 내용을 검토한 뒤 그 커밋을 기준으로 기록 → 이후 merge 통과
    assert trust_base(world.config, pushed, NoTokens(), "reviewed") == 0
    assert world.chain.last_approved() == pushed
    assert world.run(merged) == 0
    assert world.control.puts == [("site-b", 1, "pureiron-site-b-g1.pib")]
    log = (world.chain.directory / "approved.log").read_text(encoding="utf-8")
    assert "trust-base previous=" in log


def test_squash_merge_is_approved(world: World) -> None:
    head = world.open_pr(7, TWO_SITES, commits=3)
    squashed = world.fleet.squash("pr-7")
    world.github.add(
        pull(7, merge_commit_sha=squashed, head_sha=head, commits=3),
        [approve("circulation95", head)],
        squashed,
    )
    assert world.run(squashed) == 0
    assert len(world.control.puts) == 2


def test_rebase_merge_is_approved(world: World) -> None:
    head = world.open_pr(7, TWO_SITES, commits=3)
    rebased = world.fleet.rebase_onto_main("pr-7")
    assert len(rebased) == 3
    world.github.add(
        pull(7, merge_commit_sha=rebased[-1], head_sha=head, commits=3),
        [approve("circulation95", head)],
        *rebased,
    )
    assert world.run(rebased[-1]) == 0
    assert len(world.control.puts) == 2


def test_pr_cannot_add_its_own_approver(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    files = {**TWO_SITES, "allowed_approvers.txt": APPROVERS + "newbie\n"}
    merged = world.merge_pr(7, files, reviewer="newbie")
    assert world.run(merged) == 1
    assert "newbie: allowed_approvers.txt(merge 직전 main) 밖" in capsys.readouterr().out
    assert world.builder.requests == []


def test_structure_violation_reported_other_sites_published(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = world.merge_pr(
        7, {"sites/site-a.yaml": None, "sites/site-b.yaml": site_yaml("site-b", 1)}
    )
    assert world.run(merged) == 1
    assert world.control.puts == [("site-b", 1, "pureiron-site-b-g1.pib")]
    assert "::error file=sites/site-a.yaml::E_FLEET 현장 파일 삭제 금지" in capsys.readouterr().out


def test_bad_sha_and_missing_chain(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert world.run("main") == 1
    assert "40자리" in capsys.readouterr().out
    world.chain.file.unlink()
    merged = world.merge_pr(7, TWO_SITES)
    assert world.run(merged) == 1
    assert "trust-base 먼저" in capsys.readouterr().out


def test_unwritable_chain_blocks_publishing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = world.merge_pr(7, TWO_SITES)
    world.chain.file.unlink()
    world.chain.file.mkdir()  # 읽기·교체가 실패하는 자리
    assert world.run(merged) == 1
    assert world.builder.requests == []
    assert "E_APPROVAL 판정 입력을 받지 못했다" in capsys.readouterr().out


def test_defaults_are_f7_paths() -> None:
    config = Config(fleet_repo=FLEET)
    assert config.keys_dir.as_posix().endswith("/etc/pureiron/keys")
    assert config.root_pub_file.as_posix().endswith("/etc/pureiron/keys/root.pub")
    assert config.ci_token_file.as_posix().endswith("/etc/pureiron/publish/ci_token")
    assert config.viewer_token_file.as_posix().endswith("/etc/pureiron/publish/viewer_token")
    assert config.app_key_file.as_posix().endswith("/etc/pureiron/publish/app.pem")
    assert config.bundles_dir.as_posix().endswith("/srv/pureiron/bundles")
    assert config.metrics_dir.as_posix().endswith("/srv/pureiron/metrics")
    assert (
        Chain.of(config).file.as_posix().endswith("/srv/pureiron/publish/acme/fleet/last_approved")
    )
    assert config.project_repo("pureiron-project-acme") == "acme/pureiron-project-acme"


def test_cli_paths_are_overridable_by_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, sub in (
        ("PUREIRON_PUBLISH_KEYS_DIR", "keys"),
        ("PUREIRON_PUBLISH_CI_TOKEN_FILE", "ci"),
        ("PUREIRON_PUBLISH_VIEWER_TOKEN_FILE", "viewer"),
        ("PUREIRON_PUBLISH_APP_KEY_FILE", "app.pem"),
        ("PUREIRON_PUBLISH_BUNDLES_DIR", "bundles"),
        ("PUREIRON_PUBLISH_METRICS_DIR", "metrics"),
        ("PUREIRON_PUBLISH_STATE_DIR", "state"),
    ):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("PUREIRON_PUBLISH_APP_ID", "42")
    args = publish.parser().parse_args(["run", "--fleet-repo", FLEET, "--sha", "0" * 40])
    config = publish.config_from(args)
    for value in (
        config.keys_dir,
        config.ci_token_file,
        config.viewer_token_file,
        config.app_key_file,
        config.bundles_dir,
        config.metrics_dir,
        config.state_dir,
    ):
        assert value.is_relative_to(tmp_path)
    assert config.resolved_app_id() == "42"


def test_cli_without_app_key_fails_before_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = publish.main(
        [
            "run",
            "--fleet-repo",
            FLEET,
            "--sha",
            "0" * 40,
            "--app-key-file",
            str(tmp_path / "none.pem"),
            "--app-id",
            "1",
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert code == 1
    assert "E_PUBLISH GitHub App key" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


# ---------------------------------------------------------------------------- ControlApi


@pytest.fixture
def control_http() -> Generator[tuple[str, dict[str, tuple[int, object]], list[str]]]:
    responses: dict[str, tuple[int, object]] = {}
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(f"GET {self.path} {self.headers.get('Authorization')}")
            status, payload = responses.get(self.path, (500, {"detail": "unexpected"}))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self) -> None:
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            seen.append(f"PUT {self.path} {self.headers.get('Authorization')} {len(body)}")
            status, payload = responses.get(self.path, (500, {}))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/v1", responses, seen
    finally:
        server.shutdown()
        server.server_close()


def test_control_api_status_and_put(
    control_http: tuple[str, dict[str, tuple[int, object]], list[str]], tmp_path: Path
) -> None:
    base, responses, seen = control_http
    responses["/api/v1/fleet?site=site-a"] = (
        200,
        [{"site": "site-a", "max_published_gen": 3, "current": {"product": "2026.11.0"}}],
    )
    responses["/api/v1/fleet?site=site-b"] = (404, {"detail": "site 'site-b' is not registered"})
    responses["/api/v1/fleet?site=site-c"] = (403, {"detail": "role ci not allowed"})
    responses["/api/v1/bundles/site-a/4"] = (201, {"sha256": "sha256:abc"})
    api = ControlApi(base, viewer_token="view", ci_token="ci")
    assert api.site_status("site-a") == SiteStatus(3, "2026.11.0")
    assert api.site_status("site-b") is None
    with pytest.raises(ControlApiError, match="HTTP 403: role ci not allowed") as denied:
        api.site_status("site-c")
    assert denied.value.status == 403
    pib = tmp_path / "g4.pib"
    pib.write_bytes(b"x" * 3_000_000)
    assert api.put_bundle("site-a", 4, pib) == {"sha256": "sha256:abc"}
    assert seen[0] == "GET /api/v1/fleet?site=site-a Bearer view"
    assert seen[-1] == "PUT /api/v1/bundles/site-a/4 Bearer ci 3000000"


def test_put_reads_early_response_before_body_is_sent(tmp_path: Path) -> None:
    """서버가 헤더만 보고 409를 먼저 답하면(M4-0 L-7) 보내기를 멈추고 그 응답을 돌려준다."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    received = [0]

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            received[0] = len(data.partition(b"\r\n\r\n")[2])
            body = b'{"detail":"generation 4 <= latest 4"}'
            conn.sendall(
                b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            conn.settimeout(5)
            while chunk := conn.recv(1 << 20):  # 보내 온 것은 끝까지 비운다(RST 방지)
                received[0] += len(chunk)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    pib = tmp_path / "big.pib"
    size = 64 * 1024 * 1024
    with pib.open("wb") as f:
        f.truncate(size)
    api = ControlApi(f"http://127.0.0.1:{port}/api/v1", viewer_token=None, ci_token="ci")
    with pytest.raises(ControlApiError, match="HTTP 409: generation 4 <= latest 4") as conflict:
        api.put_bundle("site-a", 4, pib)
    assert conflict.value.status == 409
    thread.join(timeout=10)
    listener.close()
    assert received[0] < size
