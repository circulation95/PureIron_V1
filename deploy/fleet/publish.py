"""fleet publish 단일 진입점(M4-2 ★F1·F5·F7, foundation PD-23 ③④⑤, plan C9).

    uv run python deploy/fleet/publish.py run --fleet-repo <owner>/<fleet> --sha <sha>
    uv run python deploy/fleet/publish.py trust-base --fleet-repo <owner>/<fleet> --sha <sha>

`run`은 runner 작업 디렉터리를 믿지 않는다 — 그 sha를 GitHub API·git으로 **직접 다시 받아**
승인 판정(`approval.decide` ①~⑤, 실패 = `E_APPROVAL`, 빌드·서명·PUT 0회) → 승인 사슬 기록 →
merge 직전 main 대비 변경 파일의 구조 규칙(`E_FLEET`)·`new == old + 1`(`E_GEN`) →
**sha 트리의 모든 활성 현장**마다 `GET /fleet?site=`(viewer 토큰, 미등록이면 등록 먼저 안내 후
실패, 파일 gen == `max_published_gen`이면 최신·건너뜀, 작으면 역행 실패) → 프로젝트 repo
(`<project-owner>/<project.repo>`, 기본 owner = fleet repo owner) 태그 `version` checkout
(App 토큰) → `build_bundle.build`(같은 venv 함수 호출, 프로젝트 검증·자체 verify 포함) →
스트리밍 `PUT /bundles/{site}/{gen}`(ci 토큰, 본문 전에 온 404·409 응답도 읽는다). 현장 하나가
실패해도 나머지는 진행하고 끝에 exit 1. 드래프트 게이트는 여기서 하지 않는다(F12 — 현장
`E_DRAFT`가 최종).

workflow는 이것을 부르기만 한다. 서명 호스트를 분리할 때(★F1 전환, fleet README)는 이 명령을
sudo 뒤 전용 사용자로 옮기는 것만으로는 경계가 되지 않는다 — root 소유 설치본, 보안 설정을
root 소유 설정 파일에서만 읽는 signer 모드(argv는 `--fleet-repo`·`--sha`만), `trust-base`의
sudoers 제외가 필요하다.

경로 기본값(F7)은 모두 인자·환경변수로 덮어쓴다(테스트는 tmp만 쓴다):

    키           /etc/pureiron/keys/{release.key, release-cert.json, release-cert.sig, root.pub}
    토큰         /etc/pureiron/publish/{ci_token, viewer_token, app.pem}(0600)
    App ID       env PUREIRON_PUBLISH_APP_ID 또는 /etc/pureiron/publish/app_id
    번들·메트릭  /srv/pureiron/bundles/<site>/, /srv/pureiron/metrics/
    승인 사슬    /srv/pureiron/publish/<owner>/<repo>/{last_approved, approved.log, lock,
                 project_commits.json}
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.client
import json
import os
import secrets
import select
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.parse
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

if sys.platform != "win32":
    import fcntl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle"))

import build_bundle

from fleet.approval import Decision, Evidence, PullRequest, Review, decide, merge_base, select_pull
from fleet.fleetfiles import (
    APPROVERS_FILE,
    ActiveSite,
    ErrorCode,
    Finding,
    SiteStatus,
    SiteUpdate,
    active_sites,
    annotation,
    diff_trees,
    error,
    gen_step,
    structure,
    warning,
    workflow_command,
)
from fleet.github import (
    DEFAULT_API,
    DEFAULT_GIT_BASE,
    Git,
    GitError,
    GitHubError,
    RestGitHub,
    app_installation_token,
    is_sha,
)
from pureiron_bundle.errors import BundleRejectedError

ETC = Path("/etc/pureiron")
SRV = Path("/srv/pureiron")
DEFAULT_CONTROL_API = "http://127.0.0.1:8100/api/v1"
UPLOAD_CHUNK = 1024 * 1024
HTTP_TIMEOUT_S = 120.0
MAIN = "main"
FLEET_PERMISSIONS = {"contents": "read", "pull_requests": "read"}
PROJECT_PERMISSIONS = {"contents": "read"}


class PublishError(RuntimeError):
    pass


class ControlApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------- 설정(F7)


@dataclass(frozen=True, slots=True)
class Config:
    fleet_repo: str
    control_api: str = DEFAULT_CONTROL_API
    keys_dir: Path = ETC / "keys"
    root_pub: Path | None = None
    ci_token_file: Path = ETC / "publish" / "ci_token"
    viewer_token_file: Path = ETC / "publish" / "viewer_token"
    app_key_file: Path = ETC / "publish" / "app.pem"
    app_id: str | None = None
    app_id_file: Path = ETC / "publish" / "app_id"
    bundles_dir: Path = SRV / "bundles"
    metrics_dir: Path = SRV / "metrics"
    state_dir: Path = SRV / "publish"
    project_owner: str | None = None
    github_api: str = DEFAULT_API
    git_base: str = DEFAULT_GIT_BASE
    releases: Path = build_bundle.RELEASES
    store: Path | None = None
    main_branch: str = MAIN

    @property
    def owner(self) -> str:
        return self.fleet_repo.split("/", 1)[0]

    @property
    def root_pub_file(self) -> Path:
        return self.root_pub or self.keys_dir / "root.pub"

    def project_repo(self, name: str) -> str:
        return f"{self.project_owner or self.owner}/{name}"

    def resolved_app_id(self) -> str:
        if self.app_id:
            return self.app_id
        try:
            value = self.app_id_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PublishError(
                f"GitHub App ID 없음: env PUREIRON_PUBLISH_APP_ID 또는 {self.app_id_file}"
            ) from exc
        if not value:
            raise PublishError(f"{self.app_id_file} is empty")
        return value


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PublishError(f"token file {path} unreadable: {exc}") from exc
    if not token:
        raise PublishError(f"token file {path} is empty")
    return token


# ---------------------------------------------------------------------------- control-api HTTP


def _readable(sock: socket.socket | None) -> bool:
    if sock is None:
        return False
    ready, _, _ = select.select([sock], [], [], 0)
    return bool(ready)


@dataclass(slots=True)
class ControlApi:
    """control-api 클라이언트: `GET /fleet?site=`(viewer), `PUT /bundles/{site}/{gen}`(ci)."""

    base_url: str
    viewer_token: str | None
    ci_token: str | None
    timeout: float = HTTP_TIMEOUT_S

    def _connection(self) -> tuple[http.client.HTTPConnection, str]:
        url = urllib.parse.urlsplit(self.base_url.rstrip("/"))
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ControlApiError(f"control-api URL must be http(s)://host…: {self.base_url!r}")
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        return cls(url.hostname, url.port, timeout=self.timeout), url.path

    @staticmethod
    def _detail(body: bytes) -> str:
        try:
            parsed: object = json.loads(body)
        except ValueError:
            return body.decode("utf-8", errors="replace")[:300]
        if isinstance(parsed, dict):
            detail = cast(dict[str, object], parsed).get("detail")
            if detail is not None:
                return str(detail)
        return body.decode("utf-8", errors="replace")[:300]

    def site_status(self, site: str) -> SiteStatus | None:
        """등록 상태(미등록 404 → None)."""
        if self.viewer_token is None:
            raise ControlApiError("viewer token missing")
        conn, prefix = self._connection()
        query = urllib.parse.urlencode({"site": site})
        try:
            conn.request(
                "GET",
                f"{prefix}/fleet?{query}",
                headers={"Authorization": f"Bearer {self.viewer_token}"},
            )
            response = conn.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ControlApiError(f"GET /fleet?site={site} failed: {exc}") from exc
        finally:
            conn.close()
        if response.status == http.client.NOT_FOUND:
            return None
        if response.status != http.client.OK:
            raise ControlApiError(
                f"GET /fleet?site={site} → HTTP {response.status}: {self._detail(body)}",
                response.status,
            )
        try:
            parsed: object = json.loads(body)
        except ValueError as exc:
            raise ControlApiError(f"GET /fleet?site={site}: response is not JSON") from exc
        rows = cast(list[object], parsed) if isinstance(parsed, list) else []
        matching = [
            cast(dict[str, object], r)
            for r in rows
            if isinstance(r, dict) and cast(dict[str, object], r).get("site") == site
        ]
        if len(matching) != 1 or "max_published_gen" not in matching[0]:
            raise ControlApiError(
                f"GET /fleet?site={site}: 응답에 {site} 행이 정확히 하나 있어야 한다"
                f"({len(matching)}개)"
            )
        max_gen = matching[0]["max_published_gen"]
        current = matching[0].get("current")
        product = None
        if isinstance(current, dict):
            value = cast(dict[str, object], current).get("product")
            product = value if isinstance(value, str) else None
        return SiteStatus(
            max_published_gen=max_gen if type(max_gen) is int else None, current_product=product
        )

    def put_bundle(self, site: str, generation: int, pib: Path) -> dict[str, object]:
        """스트리밍 업로드.

        본문을 다 보내기 전에 서버가 답하면(404·409 — M4-0 L-7) 보내기를 멈추고 그 응답을 읽는다.
        """
        if self.ci_token is None:
            raise ControlApiError("ci token missing")
        size = pib.stat().st_size
        conn, prefix = self._connection()
        try:
            conn.putrequest("PUT", f"{prefix}/bundles/{site}/{generation}")
            conn.putheader("Authorization", f"Bearer {self.ci_token}")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            with (
                pib.open("rb") as source,
                contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
            ):
                while chunk := source.read(UPLOAD_CHUNK):
                    if _readable(conn.sock):
                        break  # 본문 전에 온 응답(404·409 등)
                    conn.send(chunk)
            response = conn.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ControlApiError(f"PUT /bundles/{site}/{generation} failed: {exc}") from exc
        finally:
            conn.close()
        if response.status == http.client.CREATED:
            try:
                parsed: object = json.loads(body)
            except ValueError as exc:
                raise ControlApiError(
                    f"PUT /bundles/{site}/{generation} → 201 but the body is not JSON",
                    response.status,
                ) from exc
            return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}
        raise ControlApiError(
            f"PUT /bundles/{site}/{generation} → HTTP {response.status}: {self._detail(body)}",
            response.status,
        )


# ---------------------------------------------------------------------------- 의존성 경계


class GitHubReader(Protocol):
    def pulls_for_commit(self, repo: str, sha: str) -> list[int]: ...
    def pull(self, repo: str, number: int) -> PullRequest: ...
    def reviews(self, repo: str, number: int) -> list[Review]: ...


class Control(Protocol):
    def site_status(self, site: str) -> SiteStatus | None: ...
    def put_bundle(self, site: str, generation: int, pib: Path) -> dict[str, object]: ...


class Tokens(Protocol):
    def fleet(self) -> str | None: ...
    def project(self, repo: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class BuildRequest:
    fleet_file: Path
    project_dir: Path
    out: Path
    fleet_commit: str
    site: str
    generation: int


type Builder = Callable[[BuildRequest], Path]


@dataclass(slots=True)
class AppTokens:
    """publish 호스트 App 키로 만든 설치 토큰.

    fleet repo = contents·pull_requests 읽기, 프로젝트 repo = contents 읽기.
    """

    config: Config
    _fleet: str | None = None
    _projects: dict[str, str] = field(default_factory=dict[str, str])

    def _create(self, owner: str, repo: str, permissions: Mapping[str, str]) -> str:
        try:
            pem = self.config.app_key_file.read_bytes()
        except OSError as exc:
            raise PublishError(f"GitHub App key {self.config.app_key_file} unreadable") from exc
        return app_installation_token(
            self.config.resolved_app_id(),
            pem,
            owner,
            [repo],
            permissions,
            api=self.config.github_api,
        )

    def fleet(self) -> str | None:
        if self._fleet is None:
            owner, name = self.config.fleet_repo.split("/", 1)
            self._fleet = self._create(owner, name, FLEET_PERMISSIONS)
        return self._fleet

    def project(self, repo: str) -> str | None:
        if repo not in self._projects:
            owner, name = repo.split("/", 1)
            self._projects[repo] = self._create(owner, name, PROJECT_PERMISSIONS)
        return self._projects[repo]


def bundle_builder(config: Config) -> Builder:
    """`build_bundle.build`를 같은 프로세스에서 부른다(서명 키는 config.keys_dir)."""

    def build(request: BuildRequest) -> Path:
        argv = [
            "--fleet-file",
            str(request.fleet_file),
            "--project-dir",
            str(request.project_dir),
            "--release-key",
            str(config.keys_dir / "release.key"),
            "--cert-dir",
            str(config.keys_dir),
            "--root-pub",
            str(config.root_pub_file),
            "--fleet-commit",
            request.fleet_commit,
            "--out",
            str(request.out),
            "--releases",
            str(config.releases),
        ]
        if config.store is not None:
            argv += ["--store", str(config.store)]
        if build_bundle.build(build_bundle.parser().parse_args(argv)) != 0:
            raise PublishError("bundle exceeds the size budget (build_bundle size gate)")
        return request.out / f"pureiron-{request.site}-g{request.generation}.pib"

    return build


@dataclass(slots=True)
class Deps:
    github: GitHubReader
    control: Control
    tokens: Tokens
    build: Builder
    clock: Callable[[], float] = time.monotonic


# ---------------------------------------------------------------------------- 승인 사슬


class ChainLockError(PublishError):
    pass


def _fsync_directory(directory: Path) -> None:
    """디렉터리 항목(교체한 파일 이름)을 디스크에 남긴다. Windows는 디렉터리를 열 수 없어 생략."""
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(target: Path, data: str) -> None:
    """tmp 쓰기 + fsync → 교체 → 디렉터리 fsync(전원 차단 뒤 빈 파일이 남지 않게, L5)."""
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(target)
    _fsync_directory(target.parent)


@dataclass(frozen=True, slots=True)
class Chain:
    """호스트에 기록한 마지막 승인 sha(★F2 ④). 디렉터리 = `<state>/<owner>/<repo>`.

    `run`·`trust-base`는 이 디렉터리의 `lock` 파일을 `flock`으로 잡고 돈다(publish runner는 하나지만
    잘못 둘을 등록해도 사슬 읽기·기록이 섞이지 않게, L5). 잠금은 Linux(publish 호스트)에서만 한다.
    """

    directory: Path

    @classmethod
    def of(cls, config: Config) -> Chain:
        owner, name = config.fleet_repo.split("/", 1)
        return cls(config.state_dir / owner / name)

    @property
    def file(self) -> Path:
        return self.directory / "last_approved"

    def last_approved(self) -> str | None:
        try:
            value = self.file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not is_sha(value):
            raise PublishError(f"{self.file}: not a commit sha — trust-base로 다시 기록")
        return value

    @contextlib.contextmanager
    def locked(self) -> Generator[None]:
        lock = self.directory / "lock"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle = lock.open("a", encoding="utf-8")
        except OSError as exc:
            raise ChainLockError(f"승인 사슬 잠금 파일을 열지 못했다({lock}): {exc}") from exc
        try:
            if sys.platform != "win32":
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            handle.close()  # 잠금도 함께 풀린다

    def record(self, sha: str, note: str) -> None:
        """`approved.log`에 한 줄 추가(fsync) → `last_approved` 원자적 교체.

        지금 값이 깨져 있어도(형식 오류) 덮어쓴다 — trust-base가 유일한 복구 도구다(L1).
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (self.directory / "approved.log").open("a", encoding="utf-8", newline="\n") as log:
            log.write(f"{stamp} {sha} {note}\n")
            log.flush()
            os.fsync(log.fileno())
        current = None
        with contextlib.suppress(FileNotFoundError, UnicodeDecodeError):
            current = self.file.read_text(encoding="utf-8").strip()
        if current == sha:
            return
        _write_atomic(self.file, sha + "\n")


@dataclass(frozen=True, slots=True)
class ProjectCommits:
    """`(프로젝트 repo, 태그)`마다 직전에 게시한 커밋(L2 태그 이동 감지). 파일 = 사슬 디렉터리."""

    file: Path

    @classmethod
    def of(cls, config: Config) -> ProjectCommits:
        return cls(Chain.of(config).directory / "project_commits.json")

    def _load(self) -> dict[str, str]:
        try:
            parsed: object = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise PublishError(f"{self.file} unreadable: {exc}") from exc
        if not isinstance(parsed, dict):
            raise PublishError(f"{self.file}: not a JSON object")
        items = cast(dict[str, object], parsed).items()
        return {k: v for k, v in items if isinstance(v, str)}

    def previous(self, ref: str) -> str | None:
        return self._load().get(ref)

    def record(self, ref: str, commit: str) -> str | None:
        """기록한다. 지금 파일이 깨졌으면 빈 기록으로 새로 시작해 덮어쓰고 그 사유를 돌려준다(R2-4).

        깨진 파일을 그대로 두면 매 게시마다 읽기·기록이 실패해 태그 이동 감지가 영원히 꺼진다.
        """
        reset: str | None = None
        try:
            commits = self._load()
        except PublishError as exc:
            commits, reset = {}, str(exc)
        commits[ref] = commit
        self.file.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.file, json.dumps(commits, indent=2, sort_keys=True) + "\n")
        return reset


# ---------------------------------------------------------------------------- run


def emit(finding: Finding) -> None:
    print(annotation(finding), flush=True)


def notice(message: str) -> None:
    print(workflow_command("notice", message), flush=True)


def log_traceback() -> None:
    """예상 밖 예외의 traceback을 로그로. 본문이 workflow 명령으로 해석되지 않게 막는다(N1)."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", flush=True)
    print(traceback.format_exc(), end="", flush=True)
    print(f"::{token}::", flush=True)


def fetch_fleet(config: Config, git: Git, sha: str) -> None:
    git.init()
    git.fetch(
        config.fleet_repo,
        sha,
        f"+refs/heads/{config.main_branch}:refs/remotes/origin/{config.main_branch}",
    )


def fetch_pull_head(config: Config, git: Git, pull: PullRequest) -> None:
    """PR head 커밋을 받는다(squash·rebase면 main 이력에 없다).

    sha로 받고, 안 되면 `refs/pull/<n>/head`. 끝내 없으면 `GitError`(→ `E_APPROVAL`, fail closed).
    """
    if git.has_commit(pull.head_sha):
        return
    try:
        git.fetch(config.fleet_repo, pull.head_sha)
    except GitError:
        ref = f"refs/pull/{pull.number}/head"
        git.fetch(config.fleet_repo, f"+{ref}:refs/remotes/pull/{pull.number}")
    if not git.has_commit(pull.head_sha):
        raise GitError(f"PR #{pull.number} head {pull.head_sha}를 받지 못했다")


def result_tree_matches(config: Config, git: Git, sha: str, base: str, pull: PullRequest) -> bool:
    """⑤ `sha` 트리 == `merge-tree --write-tree base head`(충돌이면 False)."""
    fetch_pull_head(config, git, pull)
    merged = git.merge_tree(base, pull.head_sha)
    return merged is not None and merged == git.tree_id(sha)


def gather(config: Config, git: Git, github: GitHubReader, sha: str, last: str | None) -> Evidence:
    """승인 판정 입력을 GitHub API·git에서 모은다(판정은 `approval.decide`)."""
    repo = config.fleet_repo
    on_main = git.is_ancestor(sha, f"refs/remotes/origin/{config.main_branch}")
    pulls = tuple(github.pull(repo, n) for n in github.pulls_for_commit(repo, sha))
    selected = select_pull(sha, pulls)
    depth = (selected.commits if selected else 1) + 1
    ancestry = git.first_parent_chain(sha, depth)
    associated: dict[str, frozenset[int]] = {}
    reviews: tuple[Review, ...] = ()
    if selected is not None:
        reviews = tuple(github.reviews(repo, selected.number))
        if selected.commits > 1 and ancestry and len(ancestry[0].parents) == 1:
            for commit in ancestry[1 : selected.commits]:
                associated[commit.sha] = frozenset(github.pulls_for_commit(repo, commit.sha))
    evidence = Evidence(
        sha=sha,
        on_main=on_main,
        ancestry=ancestry,
        pulls=pulls,
        associated=associated,
        reviews=reviews,
        approvers_text=None,
        last_approved=last,
        result_tree_matches=False,
        main_branch=config.main_branch,
    )
    if selected is None:
        return evidence
    base, _ = merge_base(evidence, selected)
    if base is None:
        return evidence
    text = git.file(base, APPROVERS_FILE)
    return dataclasses.replace(
        evidence,
        approvers_text=None if text is None else text.decode("utf-8", errors="replace"),
        result_tree_matches=result_tree_matches(config, git, sha, base, selected),
    )


class Outcome(StrEnum):
    PUBLISHED = "published"
    SKIPPED = "skipped"
    """이 커밋에서 바뀐 현장인데 그 gen이 이미 게시됨(재실행) — 경고와 함께 건너뜀."""
    CURRENT = "current"
    """이 커밋에서 안 바뀌었고 게시본이 최신."""
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SiteResult:
    site: str
    generation: int
    outcome: Outcome
    message: str


@dataclass(frozen=True, slots=True)
class SiteJob:
    """한 현장 게시 입력: 승인된 커밋 트리의 활성 현장 파일."""

    target: ActiveSite
    changed: bool
    """이 커밋(merge base 대비)에서 바뀐 파일인가."""
    content: bytes
    sha: str
    decision: Decision

    @property
    def path(self) -> str:
        return self.target.path

    @property
    def site(self) -> str:
        return self.target.site.site

    @property
    def generation(self) -> int:
        return self.target.site.generation


@dataclass(frozen=True, slots=True)
class Plan:
    """게시 대상(정합화) + 대상에서 뺀 이유."""

    targets: tuple[tuple[ActiveSite, bool], ...]
    """(활성 현장, 이 커밋에서 바뀌었나)."""
    findings: tuple[Finding, ...]


def plan_targets(
    head: Mapping[str, bytes], updates: Sequence[SiteUpdate], structural: Sequence[Finding]
) -> Plan:
    """게시 대상 = `head` 트리의 **모든** 활성 현장(승인 사슬이 트리 전체가 승인된 내용임을 보장).

    실제 게시 여부는 현장마다 `파일 gen > max_published_gen`으로 정한다(`_preflight`). 그래서 앞
    커밋에서 실패한 현장(미등록·일시 장애)도 뒤 커밋이나 main 머리 재실행에서 다시 게시된다.
    빼는 것: 이 커밋의 구조 위반(`E_FLEET`) 파일, `new == old + 1`을 어긴 바뀐 파일(`E_GEN` —
    check-gen이 빨강이어도 Free는 merge할 수 있다), 해석할 수 없는 활성 파일, 퇴역 이름을 다시 쓴
    활성 파일(뒤 둘은 트리 전체로 보므로 main에 있는 동안 매 run). `E_GEN` 제외는 그 커밋의 변경에만
    볼 수 있어 그 run만이다 — 다음 run은 정합화로 다시 본다(README "바꾸기").
    """
    excluded = {f.path for f in structural if f.is_error}
    findings: list[Finding] = []
    for update in updates:
        step = gen_step(update)
        if step is not None and update.path not in excluded:
            findings.append(step)
            excluded.add(update.path)
    sites, broken = active_sites(head)
    for finding in broken:
        if finding.path not in excluded:
            findings.append(finding)
    changed = {u.path for u in updates}
    targets = tuple((s, s.path in changed) for s in sites if s.path not in excluded)
    return Plan(targets, tuple(findings))


def _failed(job: SiteJob, message: str) -> SiteResult:
    emit(error(ErrorCode.PUBLISH, job.path, message))
    return SiteResult(job.site, job.generation, Outcome.FAILED, message)


def _preflight(deps: Deps, job: SiteJob) -> SiteResult | None:
    """`GET /fleet?site=` 확인. None = 게시 진행, 아니면 건너뜀·최신·실패 결과."""
    site, gen = job.site, job.generation
    try:
        status = deps.control.site_status(site)
    except ControlApiError as exc:
        return _failed(job, f"control-api 조회 실패: {exc}")
    if status is None:
        return _failed(
            job,
            f"현장 {site!r}이 control-api에 미등록 — 등록 먼저(pureiron-control site register) → "
            "publish workflow를 workflow_dispatch로 재실행(main 머리 기준으로 다시 맞춘다)",
        )
    published = status.max_published_gen or 0
    if published == gen:
        if not job.changed:
            return SiteResult(site, gen, Outcome.CURRENT, f"{site} gen {gen} 최신")
        message = (
            f"{site} gen {gen}은 이미 게시됨 — 건너뜀(같은 커밋 재실행이면 정상, 아니면 게시본과 "
            "내용이 다를 수 있다 — gen을 +1 한 PR로 다시 게시)"
        )
        emit(warning(job.path, message))
        return SiteResult(site, gen, Outcome.SKIPPED, message)
    if published > gen:
        return _failed(job, f"gen 역행: {site} max_published_gen {published} > 파일 gen {gen}")
    return None


@dataclass(frozen=True, slots=True)
class Built:
    pib: Path
    project_ref: str
    """`<owner>/<repo>@<tag>`."""
    project_commit: str


def _build(config: Config, deps: Deps, job: SiteJob, work: Path) -> Built:
    """프로젝트 repo 태그 checkout(App 토큰) → `build_bundle`."""
    site = job.target.site
    project_repo = config.project_repo(site.project_repo)
    project_dir = work / f"project-{site.site}"
    project_git = Git(project_dir, deps.tokens.project(project_repo), config.git_base)
    project_git.init()
    tag = site.project_version
    project_git.fetch(project_repo, f"+refs/tags/{tag}:refs/tags/{tag}", depth=1)
    project_git.checkout(f"refs/tags/{tag}")
    commit = project_git.rev_parse("HEAD")
    print(f"{site.site}: 프로젝트 {project_repo}@{tag} = {commit}", flush=True)
    fleet_file = work / f"fleet-{site.site}.yaml"
    fleet_file.write_bytes(job.content)
    pib = deps.build(
        BuildRequest(
            fleet_file=fleet_file,
            project_dir=project_dir,
            out=config.bundles_dir / site.site,
            fleet_commit=job.sha,
            site=site.site,
            generation=site.generation,
        )
    )
    return Built(pib, f"{project_repo}@{tag}", commit)


def _check_tag_moved(commits: ProjectCommits, job: SiteJob, built: Built) -> None:
    """같은 `(repo, tag)`의 직전 게시 커밋과 다르면 경고(태그를 force로 옮겼다, L2)."""
    try:
        previous = commits.previous(built.project_ref)
    except PublishError as exc:
        emit(warning(job.path, f"직전 프로젝트 커밋 기록을 읽지 못했다: {exc}"))
        return
    if previous is not None and previous != built.project_commit:
        emit(
            warning(
                job.path,
                f"프로젝트 태그 이동: {built.project_ref} 직전 게시 {previous} → 지금 "
                f"{built.project_commit}. fleet 파일은 같아도 서명되는 프로젝트 내용이 다르다 — "
                "의도한 태그 이동인지 확인",
            )
        )


BUILD_ERRORS = (
    PublishError,
    GitError,
    GitHubError,
    build_bundle.BuildError,
    BundleRejectedError,
    ValueError,
    OSError,
)


def publish_site(config: Config, deps: Deps, job: SiteJob, work: Path) -> SiteResult:
    """한 현장: 확인 → 빌드·서명 → PUT. 실패는 결과로 돌려준다(다른 현장은 계속)."""
    skipped_or_failed = _preflight(deps, job)
    if skipped_or_failed is not None:
        return skipped_or_failed
    started = deps.clock()
    try:
        built = _build(config, deps, job, work)
    except BUILD_ERRORS as exc:
        return _failed(job, f"빌드 실패: {exc}")
    commits = ProjectCommits.of(config)
    _check_tag_moved(commits, job, built)
    finished = deps.clock()
    try:
        published = deps.control.put_bundle(job.site, job.generation, built.pib)
    except ControlApiError as exc:
        hint = ""
        if exc.status == http.client.NOT_FOUND:
            hint = " — 등록 먼저 → workflow_dispatch 재실행"
        elif exc.status == http.client.CONFLICT:
            hint = " — 같은 gen이 이미 게시됐거나 gen 역행(fleet 파일 gen을 올린 PR 필요)"
        return _failed(job, f"게시 실패: {exc}{hint}")
    timings = {"build_s": finished - started, "upload_s": deps.clock() - finished}
    _write_metrics(config, job, built, published, timings)
    try:
        reset = commits.record(built.project_ref, built.project_commit)
    except (PublishError, OSError) as exc:
        emit(warning(job.path, f"프로젝트 커밋 기록을 남기지 못했다: {exc}"))
    else:
        if reset is not None:
            emit(
                warning(
                    job.path,
                    f"깨진 프로젝트 커밋 기록을 새로 시작했다(이전 태그별 기록은 버림): {reset}",
                )
            )
    size = built.pib.stat().st_size
    message = f"{job.site} gen {job.generation} 게시({built.pib.name}, {size} bytes)"
    notice(message)
    return SiteResult(job.site, job.generation, Outcome.PUBLISHED, message)


def publish_guarded(config: Config, deps: Deps, job: SiteJob, work: Path) -> SiteResult:
    """예상 밖 예외도 그 현장 `E_PUBLISH`로 기록하고 다음 현장으로 간다(F5, L4)."""
    try:
        return publish_site(config, deps, job, work)
    except Exception as exc:
        log_traceback()
        return _failed(job, f"예상 밖 오류({type(exc).__name__}): {exc}")


def _write_metrics(
    config: Config,
    job: SiteJob,
    built: Built,
    published: Mapping[str, object],
    timings: Mapping[str, float],
) -> None:
    pib = built.pib
    bundle_metrics: object = None
    with contextlib.suppress(OSError, ValueError):
        source = pib.with_name(pib.stem + build_bundle.METRICS_SUFFIX)
        bundle_metrics = json.loads(source.read_text(encoding="utf-8"))
    record: dict[str, object] = {
        "site": job.site,
        "generation": job.generation,
        "fleet_repo": config.fleet_repo,
        "fleet_commit": job.sha,
        "pull": job.decision.pull,
        "approvers": list(job.decision.approvers),
        "project_ref": built.project_ref,
        "project_commit": built.project_commit,
        "size_bytes": pib.stat().st_size,
        "sha256": published.get("sha256"),
        **{name: round(value, 3) for name, value in timings.items()},
        "bundle": bundle_metrics,
        "published_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        config.metrics_dir.mkdir(parents=True, exist_ok=True)
        target = config.metrics_dir / f"publish-{job.site}-g{job.generation}.json"
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        emit(warning("", f"publish metrics not written: {exc}"))


@contextlib.contextmanager
def _workdir() -> Generator[Path]:
    path = Path(tempfile.mkdtemp(prefix="pureiron-publish-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _approve(config: Config, deps: Deps, chain: Chain, sha: str, git: Git) -> Decision | None:
    """승인 판정 → 사슬 기록. None = 거부(이미 알렸다)."""
    try:
        last = chain.last_approved()
        fetch_fleet(config, git, sha)
        evidence = gather(config, git, deps.github, sha, last)
    except (PublishError, GitError, GitHubError, OSError) as exc:
        emit(error(ErrorCode.APPROVAL, "", f"판정 입력을 받지 못했다: {exc}"))
        return None
    decision = decide(evidence)
    if not decision.approved:
        for reason in decision.reasons:
            emit(error(ErrorCode.APPROVAL, "", reason))
        print(f"publish: {sha} 승인 거부 — 서명하지 않는다", flush=True)
        return None
    try:
        chain.record(
            sha, f"pr=#{decision.pull} base={decision.base} by={','.join(decision.approvers)}"
        )
    except OSError as exc:  # 사슬을 못 남기면 다음 판정이 깨지므로 게시하지 않는다
        emit(error(ErrorCode.PUBLISH, "", f"승인 사슬을 기록하지 못했다({chain.file}): {exc}"))
        return None
    notice(f"승인: PR #{decision.pull}, 승인자 {', '.join(decision.approvers)}")
    return decision


def _summary(results: Sequence[SiteResult], findings: Sequence[Finding]) -> int:
    current = [r.site for r in results if r.outcome is Outcome.CURRENT]
    if current:
        notice(f"게시본이 최신인 현장(변경 없음): {', '.join(current)}")
    if all(r.outcome is Outcome.CURRENT for r in results) and not findings:
        notice("새로 게시할 현장 없음")
    counts = {o: sum(r.outcome is o for r in results) for o in Outcome}
    print(
        f"publish: 게시 {counts[Outcome.PUBLISHED]}, 건너뜀 {counts[Outcome.SKIPPED]}, "
        f"최신 {counts[Outcome.CURRENT]}, 실패 {counts[Outcome.FAILED]}, "
        f"규칙 위반 {sum(f.is_error for f in findings)}",
        flush=True,
    )
    failed = any(r.outcome is Outcome.FAILED for r in results)
    return 1 if failed or any(f.is_error for f in findings) else 0


def _run_locked(config: Config, sha: str, deps: Deps, chain: Chain) -> int:
    with _workdir() as work:
        git = Git(work / "fleet", deps.tokens.fleet(), config.git_base)
        decision = _approve(config, deps, chain, sha, git)
        if decision is None:
            return 1
        assert decision.base is not None
        try:
            base_tree, head_tree = git.tree(decision.base), git.tree(sha)
        except GitError as exc:
            emit(error(ErrorCode.PUBLISH, "", f"fleet 트리를 읽지 못했다: {exc}"))
            return 1
        updates, structural = structure(diff_trees(base_tree, head_tree), head_tree)
        plan = plan_targets(head_tree, updates, structural)
        findings = [*structural, *plan.findings]
        for finding in findings:
            emit(finding)
        results = [
            publish_guarded(
                config, deps, SiteJob(target, changed, head_tree[target.path], sha, decision), work
            )
            for target, changed in plan.targets
        ]
    return _summary(results, findings)


def run(config: Config, sha: str, deps: Deps) -> int:
    """승인 판정 → 사슬 기록 → 활성 현장마다 정합화 게시. 0 = 모두 게시·최신, 1 = 실패·위반."""
    if not is_sha(sha):
        emit(error(ErrorCode.APPROVAL, "", f"--sha는 40자리 커밋 sha: {sha!r}"))
        return 1
    chain = Chain.of(config)
    try:
        with chain.locked():
            return _run_locked(config, sha, deps, chain)
    except ChainLockError as exc:
        emit(error(ErrorCode.APPROVAL, "", str(exc)))
        return 1


def _trust_base_locked(config: Config, chain: Chain, sha: str, tokens: Tokens, reason: str) -> int:
    with _workdir() as work:
        try:
            git = Git(work / "fleet", tokens.fleet(), config.git_base)
            fetch_fleet(config, git, sha)
            on_main = git.is_ancestor(sha, f"refs/remotes/origin/{config.main_branch}")
        except (PublishError, GitError, GitHubError) as exc:
            emit(error(ErrorCode.APPROVAL, "", str(exc)))
            return 1
    if not on_main:
        emit(error(ErrorCode.APPROVAL, "", f"{sha}가 {config.main_branch} 이력에 없다"))
        return 1
    try:
        previous = chain.last_approved() or "-"
    except (PublishError, OSError):
        previous = "<unreadable>"  # 깨진 기록도 덮어쓴다(L1)
    try:
        chain.record(sha, f"trust-base previous={previous} reason={reason}")
    except OSError as exc:
        emit(error(ErrorCode.APPROVAL, "", f"승인 사슬을 기록하지 못했다({chain.file}): {exc}"))
        return 1
    print(f"승인 사슬 기준 = {sha}(이전 {previous}) — {chain.file}", flush=True)
    return 0


def trust_base(config: Config, sha: str, tokens: Tokens, reason: str) -> int:
    """사람이 검토한 main 커밋을 승인 사슬 기준으로 기록한다(처음 한 번·사슬 복구)."""
    if not is_sha(sha):
        emit(error(ErrorCode.APPROVAL, "", f"--sha는 40자리 커밋 sha: {sha!r}"))
        return 1
    chain = Chain.of(config)
    try:
        with chain.locked():
            return _trust_base_locked(config, chain, sha, tokens, reason)
    except ChainLockError as exc:
        emit(error(ErrorCode.APPROVAL, "", str(exc)))
        return 1


# ---------------------------------------------------------------------------- CLI


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="publish")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "trust-base"):
        s = sub.add_parser(name)
        s.add_argument("--fleet-repo", required=True, help="<owner>/<name>")
        s.add_argument("--sha", required=True, help="fleet main 커밋 sha(40자리)")
        s.add_argument(
            "--state-dir",
            type=Path,
            default=_env_path("PUREIRON_PUBLISH_STATE_DIR", SRV / "publish"),
        )
        s.add_argument(
            "--app-key-file",
            type=Path,
            default=_env_path("PUREIRON_PUBLISH_APP_KEY_FILE", ETC / "publish" / "app.pem"),
        )
        s.add_argument("--app-id", default=os.environ.get("PUREIRON_PUBLISH_APP_ID"))
        s.add_argument("--github-api", default=os.environ.get("PUREIRON_GITHUB_API", DEFAULT_API))
        s.add_argument("--git-base", default=os.environ.get("PUREIRON_GIT_BASE", DEFAULT_GIT_BASE))
        if name == "trust-base":
            s.add_argument("--reason", default="manual", help="기록에 남길 사유(공백 없이)")
            continue
        s.add_argument(
            "--control-api", default=os.environ.get("PUREIRON_CONTROL_API", DEFAULT_CONTROL_API)
        )
        s.add_argument(
            "--keys-dir", type=Path, default=_env_path("PUREIRON_PUBLISH_KEYS_DIR", ETC / "keys")
        )
        s.add_argument("--root-pub", type=Path, default=None, help="기본 <keys-dir>/root.pub")
        s.add_argument(
            "--ci-token-file",
            type=Path,
            default=_env_path("PUREIRON_PUBLISH_CI_TOKEN_FILE", ETC / "publish" / "ci_token"),
        )
        s.add_argument(
            "--viewer-token-file",
            type=Path,
            default=_env_path(
                "PUREIRON_PUBLISH_VIEWER_TOKEN_FILE", ETC / "publish" / "viewer_token"
            ),
        )
        s.add_argument(
            "--bundles-dir",
            type=Path,
            default=_env_path("PUREIRON_PUBLISH_BUNDLES_DIR", SRV / "bundles"),
        )
        s.add_argument(
            "--metrics-dir",
            type=Path,
            default=_env_path("PUREIRON_PUBLISH_METRICS_DIR", SRV / "metrics"),
        )
        s.add_argument("--project-owner", default=None, help="기본 fleet repo owner")
        s.add_argument("--releases", type=Path, default=build_bundle.RELEASES)
        s.add_argument(
            "--store", type=Path, default=None, help="이미지 store(기본 PUREIRON_IMAGE_STORE)"
        )
    return p


def config_from(args: argparse.Namespace) -> Config:
    base = Config(
        fleet_repo=args.fleet_repo,
        state_dir=args.state_dir,
        app_key_file=args.app_key_file,
        app_id=args.app_id,
        github_api=args.github_api,
        git_base=args.git_base,
    )
    if args.command == "trust-base":
        return base
    return dataclasses.replace(
        base,
        control_api=args.control_api,
        keys_dir=args.keys_dir,
        root_pub=args.root_pub,
        ci_token_file=args.ci_token_file,
        viewer_token_file=args.viewer_token_file,
        bundles_dir=args.bundles_dir,
        metrics_dir=args.metrics_dir,
        project_owner=args.project_owner,
        releases=args.releases,
        store=args.store,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.fleet_repo.count("/") != 1:
        emit(error(ErrorCode.PUBLISH, "", f"--fleet-repo는 <owner>/<name>: {args.fleet_repo!r}"))
        return 1
    config = config_from(args)
    tokens = AppTokens(config)
    if args.command == "trust-base":
        return trust_base(config, args.sha, tokens, args.reason)
    try:
        fleet_token = tokens.fleet()
        control = ControlApi(
            config.control_api,
            viewer_token=read_token(config.viewer_token_file),
            ci_token=read_token(config.ci_token_file),
        )
    except (PublishError, GitHubError) as exc:
        emit(error(ErrorCode.PUBLISH, "", str(exc)))
        return 1
    github = RestGitHub(token=fleet_token or "", api=config.github_api)
    return run(config, args.sha, Deps(github, control, tokens, bundle_builder(config)))


if __name__ == "__main__":
    raise SystemExit(main())
