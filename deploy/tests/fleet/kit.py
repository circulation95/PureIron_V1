"""fleet 테스트 도구: 임시 git repo(원격 흉내 = 로컬 경로), 현장 파일, 가짜 GitHub·control-api.

원격 repo는 `<remotes>/<owner>/<name>` 일반 repo다 — `Git(remote_base=<remotes>)`가 경로로
fetch한다.
실제 GitHub·/etc·/srv는 쓰지 않는다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fleet.approval import APPROVED, PullRequest, Review
from fleet.fleetfiles import SiteStatus
from fleet.publish import BuildRequest, Chain, Config, Deps, run

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fleet-test",
    "GIT_AUTHOR_EMAIL": "fleet-test@example.invalid",
    "GIT_COMMITTER_NAME": "fleet-test",
    "GIT_COMMITTER_EMAIL": "fleet-test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
REPO_ROOT = Path(__file__).resolve().parents[3]
ACME = REPO_ROOT / "packages" / "project-tools" / "fixtures" / "acme"
OWNER = "acme"
FLEET = f"{OWNER}/fleet"
WINDOW = '{ recurring: { tz: Asia/Seoul, days: [sat], start: "02:00", end: "05:00" } }'
APPROVERS = "# 승인자\ncirculation95\n"


def git(repo: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True) -> str:
    """git 명령 → stdout. `check=False`면 실패해도 stdout을 돌려준다(충돌 merge 등)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **GIT_ENV, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


def site_yaml(
    site: str,
    generation: int,
    *,
    product: str = "2026.11.0",
    repo: str = "pureiron-project-acme",
    version: str = "v3",
    writes: bool | None = None,
) -> str:
    lines = [
        f"site: {site}",
        f"generation: {generation}",
        f"product: {product}",
        f"project: {{ repo: {repo}, version: {version} }}",
        f"window: {WINDOW}",
    ]
    if writes is not None:
        lines.append(f"writes: {{ enabled: {'true' if writes else 'false'} }}")
    return "\n".join(lines) + "\n"


@dataclass(slots=True)
class Repo:
    """테스트 git repo(작업 트리 있음)."""

    path: Path

    @classmethod
    def create(cls, path: Path, branch: str = "main") -> Repo:
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q", "-b", branch)
        git(path, "config", "uploadpack.allowReachableSHA1InWant", "true")
        git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
        return cls(path)

    def commit(self, files: Mapping[str, str | None], message: str = "change") -> str:
        """파일 쓰기(None = 삭제) → 커밋."""
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                git(self.path, "rm", "-q", "--", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            git(self.path, "add", "--", rel)
        git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self, ref: str = "HEAD") -> str:
        return git(self.path, "rev-parse", ref)

    def branch(self, name: str, start: str = "HEAD") -> None:
        git(self.path, "switch", "-q", "-c", name, start)

    def switch(self, name: str) -> None:
        git(self.path, "switch", "-q", name)

    def merge(self, branch: str, message: str = "Merge pull request") -> str:
        git(self.path, "merge", "-q", "--no-ff", "-m", message, branch)
        return self.head()

    def squash(self, branch: str, message: str = "Squash PR") -> str:
        git(self.path, "merge", "-q", "--squash", branch)
        git(self.path, "commit", "-q", "-m", message)
        return self.head()

    def rebase_onto_main(self, branch: str) -> list[str]:
        """branch 커밋들을 main 위에 다시 쌓는다(= GitHub rebase merge). 새 커밋 sha들."""
        before = self.head("main")
        # GitHub처럼 새 커밋이 생기게 committer 시각을 바꾼다(같으면 sha가 원본과 같아진다).
        later = {"GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z"}
        git(self.path, "cherry-pick", "--allow-empty", f"{before}..{branch}", env=later)
        return git(self.path, "rev-list", "--reverse", f"{before}..HEAD").splitlines()

    def tag(self, name: str) -> None:
        git(self.path, "tag", name)


def pull(
    number: int,
    *,
    merge_commit_sha: str | None,
    head_sha: str,
    author: str = "ws-blip",
    merged: bool = True,
    base_ref: str = "main",
    commits: int = 1,
) -> PullRequest:
    return PullRequest(
        number=number,
        author=author,
        merged=merged,
        merge_commit_sha=merge_commit_sha,
        head_sha=head_sha,
        base_ref=base_ref,
        commits=commits,
    )


def approve(reviewer: str, commit: str, state: str = APPROVED) -> Review:
    return Review(reviewer=reviewer, state=state, commit_id=commit)


@dataclass(slots=True)
class FakeGitHub:
    """`publish.GitHubReader` 가짜: 커밋 → PR 번호, PR, 리뷰."""

    commit_pulls: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    pulls: dict[int, PullRequest] = field(default_factory=dict[int, PullRequest])
    review_map: dict[int, list[Review]] = field(default_factory=dict[int, list[Review]])
    calls: list[str] = field(default_factory=list[str])

    def add(self, pr: PullRequest, reviews: list[Review], *commits: str) -> None:
        self.pulls[pr.number] = pr
        self.review_map[pr.number] = reviews
        for sha in commits:
            self.commit_pulls.setdefault(sha, []).append(pr.number)

    def pulls_for_commit(self, repo: str, sha: str) -> list[int]:
        self.calls.append(f"pulls_for_commit {repo} {sha}")
        return list(self.commit_pulls.get(sha, []))

    def pull(self, repo: str, number: int) -> PullRequest:
        self.calls.append(f"pull {repo} {number}")
        return self.pulls[number]

    def reviews(self, repo: str, number: int) -> list[Review]:
        self.calls.append(f"reviews {repo} {number}")
        return list(self.review_map.get(number, []))


@dataclass(slots=True)
class NoTokens:
    def fleet(self) -> str | None:
        return None

    def project(self, repo: str) -> str | None:
        del repo
        return None


@dataclass(slots=True)
class FakeControl:
    """`publish.Control` 가짜. `fail_put`: site → 예외."""

    status: dict[str, SiteStatus | None] = field(default_factory=dict[str, SiteStatus | None])
    fail_put: dict[str, Exception] = field(default_factory=dict[str, Exception])
    puts: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])
    lookups: list[str] = field(default_factory=list[str])

    def site_status(self, site: str) -> SiteStatus | None:
        self.lookups.append(site)
        return self.status.get(site)

    def put_bundle(self, site: str, generation: int, pib: Path) -> dict[str, object]:
        if site in self.fail_put:
            raise self.fail_put[site]
        self.puts.append((site, generation, pib.name))
        return {"sha256": "sha256:" + "0" * 64}


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
    project: Repo
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


def make_world(tmp_path: Path) -> World:
    """로컬 git 원격(fleet·프로젝트) + 가짜 GitHub·control-api·빌더. 첫 커밋 = trust-base."""
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
    return World(tmp_path, fleet, project, config, FakeGitHub(), control, FakeBuilder(), base)
