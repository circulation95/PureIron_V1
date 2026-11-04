"""github.py 가장자리: App JWT·설치 토큰, REST 페이지 넘김·파싱(가짜 GitHub HTTP), git 읽기."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fleet.approval import Review
from fleet.github import (
    Git,
    GitHubError,
    RestGitHub,
    app_installation_token,
    app_jwt,
    parse_pull,
)

from .kit import Repo, git

type Route = Callable[[str, bytes], tuple[int, dict[str, str], object]]


@dataclass(slots=True)
class FakeGitHubHttp:
    base: str
    routes: dict[tuple[str, str], Route]
    seen: list[tuple[str, str, str, bytes]] = field(
        default_factory=list[tuple[str, str, str, bytes]]
    )


@pytest.fixture
def fake_http() -> Generator[FakeGitHubHttp]:
    routes: dict[tuple[str, str], Route] = {}
    seen: list[tuple[str, str, str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append((self.command, self.path, self.headers.get("Authorization", ""), body))
            route = routes.get((self.command, self.path))
            status, headers, payload = (404, {}, {"message": "Not Found"})
            if route is not None:
                status, headers, payload = route(self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FakeGitHubHttp(f"http://127.0.0.1:{server.server_address[1]}", routes, seen)
    finally:
        server.shutdown()
        server.server_close()


def rsa_pem() -> tuple[bytes, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, key.public_key()


def unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_is_rs256_with_github_claims() -> None:
    pem, public = rsa_pem()
    token = app_jwt("12345", pem, now=1_700_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(unb64(payload)) == {
        "iat": 1_700_000_000 - 60,
        "exp": 1_700_000_000 + 540,
        "iss": "12345",
    }
    public.verify(
        unb64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_app_jwt_rejects_non_rsa_key() -> None:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(GitHubError, match="not an RSA"):
        app_jwt("1", pem, now=0)


def test_installation_token_scopes_repo_and_permissions(fake_http: FakeGitHubHttp) -> None:
    pem, _ = rsa_pem()
    fake_http.routes["GET", "/repos/acme/fleet/installation"] = lambda _p, _b: (200, {}, {"id": 77})
    fake_http.routes["POST", "/app/installations/77/access_tokens"] = lambda _p, _b: (
        201,
        {},
        {"token": "ghs_test"},
    )
    token = app_installation_token(
        "9", pem, "acme", ["fleet"], {"contents": "read"}, api=fake_http.base, now=1_700_000_000
    )
    assert token == "ghs_test"
    (_, _, auth_get, _), (_, _, auth_post, body) = fake_http.seen
    assert auth_get.startswith("Bearer ey")
    assert auth_get == auth_post
    assert json.loads(body) == {"repositories": ["fleet"], "permissions": {"contents": "read"}}


def test_rest_pages_and_parses(fake_http: FakeGitHubHttp) -> None:
    sha = "a" * 40
    page2 = f"/repos/acme/fleet/commits/{sha}/pulls?per_page=100&page=2"
    first = f"/repos/acme/fleet/commits/{sha}/pulls?per_page=100"
    fake_http.routes["GET", first] = lambda _p, _b: (
        200,
        {"Link": f'<{fake_http.base}{page2}>; rel="next"'},
        [{"number": 7}],
    )
    fake_http.routes["GET", page2] = lambda _p, _b: (200, {}, [{"number": 9}, {"x": 1}])
    pull_json: dict[str, object] = {
        "number": 7,
        "user": {"login": "ws-blip"},
        "merged": True,
        "merge_commit_sha": sha,
        "head": {"sha": "b" * 40},
        "base": {"ref": "main"},
        "commits": 2,
    }
    fake_http.routes["GET", "/repos/acme/fleet/pulls/7"] = lambda _p, _b: (200, {}, pull_json)
    reviews = [
        {"user": {"login": "circulation95"}, "state": "APPROVED", "commit_id": "b" * 40},
        {"user": None, "state": "APPROVED", "commit_id": "b" * 40},
    ]
    fake_http.routes["GET", "/repos/acme/fleet/pulls/7/reviews?per_page=100"] = lambda _p, _b: (
        200,
        {},
        reviews,
    )
    github = RestGitHub(token="ghs_x", api=fake_http.base)
    assert github.pulls_for_commit("acme/fleet", sha) == [7, 9]
    pr = github.pull("acme/fleet", 7)
    assert (pr.number, pr.author, pr.merged, pr.merge_commit_sha, pr.commits) == (
        7,
        "ws-blip",
        True,
        sha,
        2,
    )
    assert github.reviews("acme/fleet", 7) == [Review("circulation95", "APPROVED", "b" * 40)]
    assert {auth for _, _, auth, _ in fake_http.seen} == {"Bearer ghs_x"}


def test_rest_http_error_is_github_error(fake_http: FakeGitHubHttp) -> None:
    with pytest.raises(GitHubError, match="HTTP 404"):
        RestGitHub(token="t", api=fake_http.base).pull("acme/fleet", 1)


def test_simple_pull_object_is_rejected() -> None:
    simple = cast(dict[str, object], {"number": 1, "user": {"login": "a"}})
    with pytest.raises(GitHubError, match="need pulls"):
        parse_pull(simple)


def test_git_reads_trees_chains_and_fetches_by_sha(tmp_path: Path) -> None:
    remote = Repo.create(tmp_path / "remotes" / "acme" / "fleet")
    first = remote.commit({"sites/site-a.yaml": "a1\n", "README.md": "r"}, "one")
    second = remote.commit({"sites/site-a.yaml": "a2\n", "sites/.retired/.gitkeep": ""}, "two")
    local = Git(tmp_path / "work", remote_base=str(tmp_path / "remotes"))
    local.init()
    local.fetch("acme/fleet", second, "+refs/heads/main:refs/remotes/origin/main")
    assert local.tree(second) == {"sites/.retired/.gitkeep": b"", "sites/site-a.yaml": b"a2\n"}
    assert local.file(first, "sites/site-a.yaml") == b"a1\n"
    assert local.file(first, "sites/missing.yaml") is None
    chain = local.first_parent_chain(second, 5)
    assert [c.sha for c in chain] == [second, first]
    assert chain[0].parents == (first,)
    assert local.is_ancestor(first, "refs/remotes/origin/main")
    assert not local.is_ancestor(second, first)
    assert git(remote.path, "rev-parse", "HEAD") == local.rev_parse(second)
