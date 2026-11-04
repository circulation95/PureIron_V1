"""fleet publish 단일 진입점(M4-2 ★F1·F5·F7, foundation PD-23 ③④⑤, plan C9).

    uv run python deploy/fleet/publish.py run --fleet-repo <owner>/<fleet> --sha <sha>
    uv run python deploy/fleet/publish.py trust-base --fleet-repo <owner>/<fleet> --sha <sha>

`run`은 runner 작업 디렉터리를 믿지 않는다 — 그 sha를 GitHub API·git으로 **직접 다시 받아**
승인 판정(`approval.decide`, 실패 = `E_APPROVAL`, 빌드·서명·PUT 0회) → 승인 사슬 기록 →
변경 현장 파일(merge 직전 main 대비) → 현장마다 `GET /fleet?site=`(viewer 토큰, 미등록이면 등록 먼저
안내 후 실패, `max_published_gen == gen`이면 이미 게시됨으로 건너뜀) → 프로젝트 repo
(`<project-owner>/<project.repo>`, 기본 owner = fleet repo owner) 태그 `version` checkout
(App 토큰) →
`build_bundle.build`(같은 venv 함수 호출, 프로젝트 검증·자체 verify 포함) → 스트리밍
`PUT /bundles/{site}/{gen}`(ci 토큰, 본문 전에 온 404·409 응답도 읽는다). 현장 하나가 실패해도
나머지는 진행하고 끝에 exit 1. 드래프트 게이트는 여기서 하지 않는다(F12 — 현장 `E_DRAFT`가 최종).

workflow는 이것을 부르기만 한다. 서명 호스트를 분리할 때(★F1 전환 조건, fleet README)는 이 명령을
sudo 뒤 전용 사용자로 옮기면 되고 코드는 바뀌지 않는다.

경로 기본값(F7)은 모두 인자·환경변수로 덮어쓴다(테스트는 tmp만 쓴다):

    키           /etc/pureiron/keys/{release.key, release-cert.json, release-cert.sig, root.pub}
    토큰         /etc/pureiron/publish/{ci_token, viewer_token, app.pem}(0600)
    App ID       env PUREIRON_PUBLISH_APP_ID 또는 /etc/pureiron/publish/app_id
    번들·메트릭  /srv/pureiron/bundles/<site>/, /srv/pureiron/metrics/
    승인 사슬    /srv/pureiron/publish/<owner>/<repo>/{last_approved, approved.log}
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.client
import json
import os
import select
import shutil
import socket
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle"))

import build_bundle

from fleet.approval import Decision, Evidence, PullRequest, Review, decide, merge_base, select_pull
from fleet.fleetfiles import (
    APPROVERS_FILE,
    ErrorCode,
    Finding,
    SiteStatus,
    SiteUpdate,
    annotation,
    diff_trees,
    error,
    structure,
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
            rows = cast(list[dict[str, object]], json.loads(body))
            row = rows[0]
            max_gen = row["max_published_gen"]
            current = row.get("current")
        except (ValueError, LookupError, TypeError) as exc:
            raise ControlApiError(f"GET /fleet?site={site}: unexpected response") from exc
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
            parsed: object = json.loads(body)
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


@dataclass(frozen=True, slots=True)
class Chain:
    """호스트에 기록한 마지막 승인 sha(★F2 ④). 디렉터리 = `<state>/<owner>/<repo>`."""

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

    def record(self, sha: str, note: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (self.directory / "approved.log").open("a", encoding="utf-8", newline="\n") as log:
            log.write(f"{stamp} {sha} {note}\n")
        if self.last_approved() == sha:
            return
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(sha + "\n", encoding="utf-8", newline="\n")
        tmp.replace(self.file)


# ---------------------------------------------------------------------------- run


def emit(finding: Finding) -> None:
    print(annotation(finding), flush=True)


def notice(message: str) -> None:
    print(f"::notice::{message}", flush=True)


def fetch_fleet(config: Config, git: Git, sha: str) -> None:
    git.init()
    git.fetch(
        config.fleet_repo,
        sha,
        f"+refs/heads/{config.main_branch}:refs/remotes/origin/{config.main_branch}",
    )


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
        main_branch=config.main_branch,
    )
    if selected is None:
        return evidence
    base, _ = merge_base(evidence, selected)
    if base is None:
        return evidence
    text = git.file(base, APPROVERS_FILE)
    return dataclasses.replace(
        evidence, approvers_text=None if text is None else text.decode("utf-8", errors="replace")
    )


class Outcome(StrEnum):
    PUBLISHED = "published"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SiteResult:
    site: str
    generation: int
    outcome: Outcome
    message: str


@dataclass(frozen=True, slots=True)
class SiteJob:
    """한 현장 게시 입력: 승인된 커밋의 현장 파일 갱신."""

    update: SiteUpdate
    content: bytes
    sha: str
    decision: Decision

    @property
    def site(self) -> str:
        return self.update.site.site

    @property
    def generation(self) -> int:
        return self.update.site.generation


def _failed(job: SiteJob, message: str) -> SiteResult:
    emit(error(ErrorCode.PUBLISH, job.update.path, message))
    return SiteResult(job.site, job.generation, Outcome.FAILED, message)


def _preflight(deps: Deps, job: SiteJob) -> SiteResult | None:
    """`GET /fleet?site=` 확인. None = 게시 진행, 아니면 건너뜀·실패 결과."""
    site, gen = job.site, job.generation
    try:
        status = deps.control.site_status(site)
    except ControlApiError as exc:
        return _failed(job, f"control-api 조회 실패: {exc}")
    if status is None:
        return _failed(
            job,
            f"현장 {site!r}이 control-api에 미등록 — 등록 먼저(pureiron-control site register) → "
            "이 publish workflow를 workflow_dispatch로 재실행",
        )
    published = status.max_published_gen or 0
    if published == gen:
        message = f"{site} gen {gen}은 이미 게시됨 — 건너뜀"
        notice(message)
        return SiteResult(site, gen, Outcome.SKIPPED, message)
    if published > gen:
        return _failed(job, f"gen 역행: {site} max_published_gen {published} > 파일 gen {gen}")
    return None


def _build(config: Config, deps: Deps, job: SiteJob, work: Path) -> Path:
    """프로젝트 repo 태그 checkout(App 토큰) → `build_bundle`."""
    site = job.update.site
    project_repo = config.project_repo(site.project_repo)
    project_dir = work / f"project-{site.site}"
    project_git = Git(project_dir, deps.tokens.project(project_repo), config.git_base)
    project_git.init()
    tag = site.project_version
    project_git.fetch(project_repo, f"+refs/tags/{tag}:refs/tags/{tag}", depth=1)
    project_git.checkout(f"refs/tags/{tag}")
    print(
        f"{site.site}: 프로젝트 {project_repo}@{tag} = {project_git.rev_parse('HEAD')}", flush=True
    )
    fleet_file = work / f"fleet-{site.site}.yaml"
    fleet_file.write_bytes(job.content)
    return deps.build(
        BuildRequest(
            fleet_file=fleet_file,
            project_dir=project_dir,
            out=config.bundles_dir / site.site,
            fleet_commit=job.sha,
            site=site.site,
            generation=site.generation,
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
        pib = _build(config, deps, job, work)
    except BUILD_ERRORS as exc:
        return _failed(job, f"빌드 실패: {exc}")
    built = deps.clock()
    try:
        published = deps.control.put_bundle(job.site, job.generation, pib)
    except ControlApiError as exc:
        hint = ""
        if exc.status == http.client.NOT_FOUND:
            hint = " — 등록 먼저 → workflow_dispatch 재실행"
        elif exc.status == http.client.CONFLICT:
            hint = " — 같은 gen이 이미 게시됐거나 gen 역행(fleet 파일 gen을 올린 PR 필요)"
        return _failed(job, f"게시 실패: {exc}{hint}")
    timings = {"build_s": built - started, "upload_s": deps.clock() - built}
    _write_metrics(config, job, pib, published, timings)
    message = f"{job.site} gen {job.generation} 게시({pib.name}, {pib.stat().st_size} bytes)"
    notice(message)
    return SiteResult(job.site, job.generation, Outcome.PUBLISHED, message)


def _write_metrics(
    config: Config,
    job: SiteJob,
    pib: Path,
    published: Mapping[str, object],
    timings: Mapping[str, float],
) -> None:
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
        print(f"::warning::publish metrics not written: {exc}", flush=True)


@contextlib.contextmanager
def _workdir() -> Generator[Path]:
    path = Path(tempfile.mkdtemp(prefix="pureiron-publish-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run(config: Config, sha: str, deps: Deps) -> int:
    """승인 판정 → 사슬 기록 → 현장마다 게시. 0 = 모두 게시·건너뜀, 1 = 하나라도 실패."""
    if not is_sha(sha):
        emit(error(ErrorCode.APPROVAL, "", f"--sha는 40자리 커밋 sha: {sha!r}"))
        return 1
    chain = Chain.of(config)
    with _workdir() as work:
        try:
            last = chain.last_approved()
            git = Git(work / "fleet", deps.tokens.fleet(), config.git_base)
            fetch_fleet(config, git, sha)
            evidence = gather(config, git, deps.github, sha, last)
        except (PublishError, GitError, GitHubError) as exc:
            emit(error(ErrorCode.APPROVAL, "", f"판정 입력을 받지 못했다: {exc}"))
            return 1
        decision = decide(evidence)
        if not decision.approved:
            for reason in decision.reasons:
                emit(error(ErrorCode.APPROVAL, "", reason))
            print(f"publish: {sha} 승인 거부 — 서명하지 않는다", flush=True)
            return 1
        assert decision.base is not None
        chain.record(
            sha, f"pr=#{decision.pull} base={decision.base} by={','.join(decision.approvers)}"
        )
        notice(f"승인: PR #{decision.pull}, 승인자 {', '.join(decision.approvers)}")
        try:
            base_tree, head_tree = git.tree(decision.base), git.tree(sha)
        except GitError as exc:
            emit(error(ErrorCode.PUBLISH, "", f"fleet 트리를 읽지 못했다: {exc}"))
            return 1
        updates, findings = structure(diff_trees(base_tree, head_tree), head_tree)
        for finding in findings:
            emit(finding)
        results = [
            publish_site(config, deps, SiteJob(u, head_tree[u.path], sha, decision), work)
            for u in updates
        ]
    if not updates and not findings:
        notice("변경된 현장 파일 없음 — 게시할 것이 없다")
    failed = [r for r in results if r.outcome is Outcome.FAILED]
    counts = {o: sum(r.outcome is o for r in results) for o in Outcome}
    print(
        f"publish: 게시 {counts[Outcome.PUBLISHED]}, 건너뜀 {counts[Outcome.SKIPPED]}, "
        f"실패 {counts[Outcome.FAILED]}, 구조 위반 {sum(f.is_error for f in findings)}",
        flush=True,
    )
    return 1 if failed or any(f.is_error for f in findings) else 0


def trust_base(config: Config, sha: str, tokens: Tokens, reason: str) -> int:
    """사람이 검토한 main 커밋을 승인 사슬 기준으로 기록한다(처음 한 번·사슬 복구)."""
    if not is_sha(sha):
        print(f"::error::{ErrorCode.APPROVAL} --sha는 40자리 커밋 sha: {sha!r}")
        return 1
    with _workdir() as work:
        try:
            git = Git(work / "fleet", tokens.fleet(), config.git_base)
            fetch_fleet(config, git, sha)
            if not git.is_ancestor(sha, f"refs/remotes/origin/{config.main_branch}"):
                print(f"::error::{ErrorCode.APPROVAL} {sha}가 {config.main_branch} 이력에 없다")
                return 1
        except (PublishError, GitError, GitHubError) as exc:
            print(f"::error::{ErrorCode.APPROVAL} {exc}")
            return 1
    chain = Chain.of(config)
    previous = chain.last_approved()
    chain.record(sha, f"trust-base previous={previous or '-'} reason={reason}")
    print(f"승인 사슬 기준 = {sha}(이전 {previous or '없음'}) — {chain.file}")
    return 0


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
        print(f"::error::--fleet-repo는 <owner>/<name>: {args.fleet_repo!r}")
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
        print(f"::error::{ErrorCode.PUBLISH} {exc}")
        return 1
    github = RestGitHub(token=fleet_token or "", api=config.github_api)
    return run(config, args.sha, Deps(github, control, tokens, bundle_builder(config)))


if __name__ == "__main__":
    raise SystemExit(main())
