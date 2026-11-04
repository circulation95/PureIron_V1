"""GitHub REST·git 가장자리(M4-2 F10): 승인 판정 입력을 그 sha로 직접 다시 받는다.

- `RestGitHub`: `commits/{sha}/pulls`, `pulls/{n}`, `pulls/{n}/reviews`(Link 헤더 페이지 넘김),
  토큰 주입.
- `app_installation_token`: GitHub App 키(PEM)로 JWT(RS256) → 설치 토큰(지정 repo·권한만).
  publish 호스트의 App 키(`/etc/pureiron/publish/app.pem`)로 fleet·프로젝트 repo 읽기 토큰을 만든다.
- `Git`: 로컬 git 명령(트리·파일·첫 부모 이력·조상 판정·fetch). 토큰은 argv가 아니라 env
  (`GIT_CONFIG_*` → `http.extraHeader`)로만 넘긴다.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from fleet.approval import Commit, PullRequest, Review

DEFAULT_API = "https://api.github.com"
DEFAULT_GIT_BASE = "https://github.com"
TIMEOUT_S = 30.0
MAX_PAGES = 50
JWT_LIFETIME_S = 540
"""App JWT 수명(GitHub 상한 10분, 시계 오차 여유)."""
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_SHA = re.compile(r"^[0-9a-f]{40}$")

type JsonObject = dict[str, object]


class GitHubError(RuntimeError):
    pass


class GitError(RuntimeError):
    pass


def is_sha(value: str) -> bool:
    return _SHA.fullmatch(value) is not None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, private_key_pem: bytes, now: int) -> str:
    """GitHub App JWT(RS256, iat = now - 60, exp = now + 9분)."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubError("GitHub App key is not an RSA private key")
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + JWT_LIFETIME_S, "iss": app_id}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: object


def _request(
    url: str, *, token: str, method: str = "GET", body: object = None, scheme: str = "Bearer"
) -> Response:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Authorization", f"{scheme} {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
            status = int(response.status)
            headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GitHubError(f"{method} {url} → HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubError(f"{method} {url} failed: {exc}") from exc
    try:
        parsed: object = json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise GitHubError(f"{method} {url}: response is not JSON") from exc
    return Response(status, headers, parsed)


def app_installation_token(
    app_id: str,
    private_key_pem: bytes,
    owner: str,
    repositories: Sequence[str],
    permissions: Mapping[str, str],
    *,
    api: str = DEFAULT_API,
    now: int | None = None,
) -> str:
    """App 설치 토큰(지정 repo·권한만). 설치는 `repositories[0]`로 찾는다(같은 owner)."""
    jwt = app_jwt(app_id, private_key_pem, int(time.time()) if now is None else now)
    found = _request(f"{api}/repos/{owner}/{repositories[0]}/installation", token=jwt)
    installation = _object(found.body).get("id")
    if type(installation) is not int:
        raise GitHubError(f"no App installation id for {owner}/{repositories[0]}")
    created = _request(
        f"{api}/app/installations/{installation}/access_tokens",
        token=jwt,
        method="POST",
        body={"repositories": list(repositories), "permissions": dict(permissions)},
    )
    token = _object(created.body).get("token")
    if not isinstance(token, str) or not token:
        raise GitHubError("App installation token response has no token")
    return token


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise GitHubError("expected a JSON object")
    return cast(JsonObject, value)


def _str(data: JsonObject, *path: str) -> str:
    value: object = data
    for key in path:
        value = _object(value).get(key)
    if not isinstance(value, str):
        raise GitHubError(f"GitHub response field {'.'.join(path)} is not a string")
    return value


def _optional_str(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _commit_sha(value: str, field_name: str) -> str:
    """API가 준 커밋 sha는 40자리 hex만 받는다 — git 인자로 넘어가므로 옵션 주입을 막는다(R2-5)."""
    if not is_sha(value):
        raise GitHubError(f"GitHub response field {field_name} is not a commit sha: {value[:80]!r}")
    return value


def parse_pull(data: JsonObject) -> PullRequest:
    commits = data.get("commits")
    merged = data.get("merged")
    if type(commits) is not int or not isinstance(merged, bool):
        raise GitHubError("pull request object lacks commits/merged (need pulls/{n})")
    number = data.get("number")
    if type(number) is not int:
        raise GitHubError("pull request has no number")
    merge_commit_sha = _optional_str(data, "merge_commit_sha")
    return PullRequest(
        number=number,
        author=_str(data, "user", "login"),
        merged=merged,
        merge_commit_sha=(
            None if merge_commit_sha is None else _commit_sha(merge_commit_sha, "merge_commit_sha")
        ),
        head_sha=_commit_sha(_str(data, "head", "sha"), "head.sha"),
        base_ref=_str(data, "base", "ref"),
        commits=commits,
    )


def parse_review(data: JsonObject) -> Review | None:
    """리뷰 한 건(탈퇴 사용자처럼 user가 없으면 None)."""
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    login = cast(JsonObject, user).get("login")
    state = data.get("state")
    if not isinstance(login, str) or not isinstance(state, str):
        return None
    return Review(reviewer=login, state=state, commit_id=_optional_str(data, "commit_id"))


@dataclass(slots=True)
class RestGitHub:
    """승인 판정용 GitHub REST 읽기(`approval.Evidence` 재료)."""

    token: str
    api: str = DEFAULT_API

    def _pages(self, path: str) -> list[JsonObject]:
        url: str | None = f"{self.api}{path}{'&' if '?' in path else '?'}per_page=100"
        items: list[JsonObject] = []
        for _ in range(MAX_PAGES):
            if url is None:
                return items
            response = _request(url, token=self.token)
            body: object = response.body
            if not isinstance(body, list):
                raise GitHubError(f"GET {url}: expected a JSON array")
            items.extend(_object(item) for item in cast(list[object], body))
            match = _NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
        raise GitHubError(f"GET {path}: more than {MAX_PAGES} pages")

    def pulls_for_commit(self, repo: str, sha: str) -> list[int]:
        numbers: list[int] = []
        for item in self._pages(f"/repos/{repo}/commits/{sha}/pulls"):
            number = item.get("number")
            if type(number) is int:
                numbers.append(number)
        return numbers

    def pull(self, repo: str, number: int) -> PullRequest:
        return parse_pull(
            _object(_request(f"{self.api}/repos/{repo}/pulls/{number}", token=self.token).body)
        )

    def reviews(self, repo: str, number: int) -> list[Review]:
        found = (
            parse_review(item) for item in self._pages(f"/repos/{repo}/pulls/{number}/reviews")
        )
        return [r for r in found if r is not None]


def _auth_env(token: str | None, git_base: str) -> dict[str, str]:
    if token is None:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{git_base}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


@dataclass(slots=True)
class Git:
    """한 로컬 repo의 git 명령. `remote_base`는 원격 URL 앞부분(기본 https://github.com)."""

    directory: Path
    token: str | None = None
    remote_base: str = DEFAULT_GIT_BASE
    env: dict[str, str] = field(default_factory=dict[str, str])

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            **self.env,
            **_auth_env(self.token, self.remote_base),
        }
        result = subprocess.run(
            ["git", "-C", str(self.directory), *args], env=env, capture_output=True, check=False
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} exited {result.returncode}: {stderr}")
        return result

    def text(self, *args: str) -> str:
        return self._run(*args).stdout.decode("utf-8").strip()

    def remote_url(self, repo: str) -> str:
        return (
            f"{self.remote_base}/{repo}.git"
            if "://" in self.remote_base
            else (str(Path(self.remote_base) / repo))
        )

    def init(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q")

    def fetch(self, repo: str, *refspecs: str, depth: int | None = None) -> None:
        extra = [f"--depth={depth}"] if depth is not None else []
        self._run(
            "fetch", "-q", "--no-tags", *extra, "--end-of-options", self.remote_url(repo), *refspecs
        )

    def checkout(self, ref: str) -> None:
        # git 2.43 checkout은 --end-of-options를 받지 않는다 → rev-parse(받음)로 sha를 구해 넘긴다.
        commit = self.rev_parse(ref)
        self._run("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit)

    def rev_parse(self, ref: str) -> str:
        return self.text("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(
            "merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant, check=False
        )
        if result.returncode not in (0, 1):
            raise GitError(f"git merge-base --is-ancestor failed: {result.stderr!r}")
        return result.returncode == 0

    def has_commit(self, sha: str) -> bool:
        return (
            self._run(
                "cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}", check=False
            ).returncode
            == 0
        )

    def tree_id(self, sha: str) -> str:
        return self.text("rev-parse", "--verify", "--end-of-options", f"{sha}^{{tree}}")

    def merge_tree(self, base: str, head: str) -> str | None:
        """`git merge-tree --write-tree base head`의 결과 트리(충돌이면 None). git ≥ 2.38 필요."""
        result = self._run(
            "merge-tree",
            "--write-tree",
            "--no-messages",
            "--end-of-options",
            base,
            head,
            check=False,
        )
        conflicted = result.returncode == 1 and bool(result.stdout.strip())
        if conflicted:  # 충돌이면 exit 1 + 트리 id. 객체를 못 찾아도 exit 1(stdout 없음) → 오류
            return None
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            version = self._run("version", check=False).stdout.decode("utf-8", "replace").strip()
            raise GitError(
                f"git merge-tree --write-tree 실패(exit {result.returncode}, git ≥ 2.38 필요, "
                f"현재 {version or '알 수 없음'}): {stderr}"
            )
        lines = result.stdout.decode("ascii", errors="replace").splitlines()
        if not lines or not is_sha(lines[0].strip()):
            raise GitError(f"git merge-tree --write-tree: 트리 id를 읽지 못했다 {lines[:1]!r}")
        return lines[0].strip()

    def first_parent_chain(self, sha: str, count: int) -> tuple[Commit, ...]:
        out = self.text(
            "rev-list",
            "--first-parent",
            "--parents",
            f"--max-count={count}",
            "--end-of-options",
            sha,
        )
        commits: list[Commit] = []
        for line in out.splitlines():
            shas = line.split()
            commits.append(Commit(sha=shas[0], parents=tuple(shas[1:])))
        return tuple(commits)

    def file(self, sha: str, path: str) -> bytes | None:
        result = self._run("cat-file", "blob", "--end-of-options", f"{sha}:{path}", check=False)
        return result.stdout if result.returncode == 0 else None

    def tree(self, sha: str, prefix: str = "sites") -> dict[str, bytes]:
        """`sha`의 `prefix/` 아래 일반 파일 {경로: 바이트}."""
        listing = self._run(
            "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", sha, "--", prefix
        ).stdout
        tree: dict[str, bytes] = {}
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, kind, _ = meta.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
            if kind != "blob" or mode not in ("100644", "100755"):
                tree[path] = (
                    b""  # 심볼릭 링크·서브모듈: 내용 대신 빈 값(구조 규칙이 이름으로 거른다)
                )
                continue
            blob = self.file(sha, path)
            if blob is None:
                raise GitError(f"cannot read {sha}:{path}")
            tree[path] = blob
        return tree
