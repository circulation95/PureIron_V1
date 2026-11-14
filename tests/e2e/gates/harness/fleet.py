"""fleet 경로의 무인 GitHub 조작(M6-4a A1, 계획 P7·★D5) — fleet-test·acme-test·PureIron_V1.

`Gh`는 계정 하나로 `gh` 한 번을 부른다. **토큰은 부르는 순간에** `gh auth token -u <계정>`으로
읽어 그 `gh` 자식 프로세스 하나의 환경변수 `GH_TOKEN`에만 싣는다 — 인자(argv)·`Context`·
`state.json`·결과 파일·로그·예외 문자열에 남기지 않는다(예외·출력 문자열은 토큰을 지운 뒤에만
쓴다). 바깥 환경의 `GH_TOKEN`·`GITHUB_TOKEN`은 자식에 넘기지 않는다(`nightly.SECRET_ENV`와 같은
목록). 값을 보내는 호출(요청 본문·secret 값)은 stdin으로만 넘긴다.

작성은 ws-blip, 승인·merge는 circulation95(★D5 — 사람과 기계가 계정으로 구분되지 않는다. bot PR
제목 `[g2-nightly <run>]`, 브랜치 `g2/`로 가른다). **merge 전에 `pulls/N/reviews`에서 head sha와
일치하는 승인(리뷰어별 마지막 상태가 APPROVED, `commit_id == head`)을 확인한다** — GitHub Free는
승인 없는 merge를 막지 않는다(M4에서 2번). 확인 없이 merge하는 길은 `E_APPROVAL` 시험용
`open_probe` → `merge_unapproved` 하나뿐이다(그 PR 번호만 받는다).

GitHub 호출 실패·대기 시간 초과는 `FleetError`(`InfraError` — 실행 환경)다. 판정(승인 거부·게시
실패 해석)은 부르는 쪽이 `publish_outcome` 같은 순수 함수로 한다.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from harness.context import InfraError

FLEET_REPO = "circulation95/pureiron-fleet-test"
PROJECT_REPO = "circulation95/pureiron-project-acme-test"
PROJECT_NAME = PROJECT_REPO.split("/", 1)[1]
"""fleet 현장 파일 `project.repo`(owner는 fleet repo owner)."""
V1_REPO = "circulation95/PureIron_V1"
AUTHOR = "ws-blip"
APPROVER = "circulation95"
MAIN = "main"
BOT_TITLE = "[g2-nightly"
"""bot PR 제목 접두 — 이것으로 시작하지 않는 열린 PR은 사람 것이다(R5)."""
BOT_BRANCH = "g2/"
PUBLISH_WORKFLOW = "publish.yml"
CHECK_GEN = "check-gen"
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
"""자식에 넘기지 않는 바깥 토큰(`nightly.SECRET_ENV`와 같다)."""
GH_TIMEOUT_S = 60.0
"""`gh` 한 번(로그 받기는 `LOG_TIMEOUT_S`)."""
LOG_TIMEOUT_S = 180.0
POLL_S = 5.0
CHECK_TIMEOUT_S = 8 * 60.0
"""check-gen이 끝날 때까지 — M4 마감 실측 2에서 runner 배정 전 queued가 4분 넘었다(K3)."""
RUN_APPEAR_TIMEOUT_S = 3 * 60.0
"""push(merge·되돌림) 뒤 publish run이 생길 때까지."""
RUN_TIMEOUT_S = 15 * 60.0
"""publish run 하나(runner 대기 + 번들 빌드 2개 + PUT) — M4 마감 merge → run 완료 55초."""
REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
"""리뷰어 상태를 바꾸는 리뷰(publish `approval.py` ②와 같은 규칙 — COMMENTED·PENDING은 무시)."""


class FleetError(InfraError):
    """GitHub 호출 실패·대기 시간 초과(실행 환경)."""


class NotFoundError(FleetError):
    """HTTP 404."""


GhRun = Callable[
    [Sequence[str], str | None, Mapping[str, str], float], "subprocess.CompletedProcess[str]"
]
"""(`gh` 뒤 인자, stdin, 환경, 시간 제한) → 끝난 프로세스. 단위 시험이 가짜로 바꾼다."""


def run_gh(
    args: Sequence[str], stdin: str | None, env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        input=stdin,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_s,
        check=False,
    )


def scrub(text: str, secret: str) -> str:
    """`text`에서 `secret`을 지운다(빈 값이면 그대로)."""
    return text.replace(secret, "***") if secret else text


class Gh:
    """계정별 `gh` 호출. 토큰은 호출마다 새로 읽고 그 호출의 자식 환경에만 둔다(이 객체에 남기지
    않는다)."""

    def __init__(self, run: GhRun = run_gh, environ: Mapping[str, str] | None = None) -> None:
        self._run = run
        source = os.environ if environ is None else environ
        self._base = {k: v for k, v in source.items() if k.upper() not in SECRET_ENV}

    def _token(self, account: str) -> str:
        try:
            done = self._run(["auth", "token", "-u", account], None, self._base, GH_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FleetError(f"gh auth token -u {account} failed: {type(exc).__name__}") from None
        token = done.stdout.strip()
        if done.returncode != 0 or not token:
            # stdout은 토큰일 수 있다 — 메시지에는 종료 코드와 stderr 끝(토큰을 지운 것)만
            tail = scrub(done.stderr.strip(), token)[-200:]
            raise FleetError(f"gh auth token -u {account} exited {done.returncode}: {tail}")
        return token

    def call(
        self,
        account: str,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_s: float = GH_TIMEOUT_S,
    ) -> str:
        """`gh <args>`를 `account` 토큰으로. 실패는 `FleetError`(HTTP 404는 `NotFoundError`)."""
        token = self._token(account)
        env = {**self._base, "GH_TOKEN": token}
        head = " ".join(args[:4])
        try:
            done = self._run(args, stdin, env, timeout_s)
        except subprocess.TimeoutExpired:
            raise FleetError(f"gh {head} timed out after {timeout_s:.0f}s") from None
        except OSError as exc:
            raise FleetError(scrub(f"gh {head} failed: {exc}", token)) from None
        finally:
            del env
        if done.returncode != 0:
            tail = scrub(done.stderr.strip(), token)[-300:]
            error = NotFoundError if "(HTTP 404)" in done.stderr else FleetError
            raise error(f"gh {head} exited {done.returncode} as {account}: {tail}")
        return scrub(done.stdout, token)

    def api(self, account: str, method: str, path: str, body: object | None = None) -> object:
        """`gh api -X <method> <path>`(본문은 stdin JSON). 빈 응답은 None."""
        args = ["api", "-X", method, path]
        if body is not None:
            args += ["--input", "-"]
        out = self.call(account, args, stdin=None if body is None else json.dumps(body))
        return json.loads(out) if out.strip() else None


# ---------------------------------------------------------------- 순수 판단


def approved_at_head(reviews: Sequence[Mapping[str, object]], head: str, approver: str) -> bool:
    """`approver`의 마지막 상태 리뷰(APPROVED·CHANGES_REQUESTED·DISMISSED, 제출 순서)가 APPROVED이고
    그 `commit_id`가 `head`인가 — publish 승인 판정 ②와 같은 규칙. 오래된 승인(그 뒤 커밋이 붙음)은
    승인이 아니다. login은 대소문자를 무시한다."""
    if not head:
        return False
    last: Mapping[str, object] | None = None
    for review in reviews:
        user = cast(Mapping[str, object], review.get("user") or {})
        if str(user.get("login") or "").lower() != approver.lower():
            continue
        if review.get("state") in REVIEW_STATES:
            last = review
    return last is not None and last.get("state") == "APPROVED" and last.get("commit_id") == head


SUMMARY = re.compile(r"publish: 게시 (\d+), 건너뜀 (\d+), 최신 (\d+), 실패 (\d+), 규칙 위반 (\d+)")
SITE_ID = r"(?<![a-z0-9-])([a-z0-9][a-z0-9-]{0,62})"
"""fleet 현장 id(파일 이름 규칙) — 앞 글자가 id 문자가 아닐 때만(`::notice::site-a`)."""
PUBLISHED = re.compile(SITE_ID + r" gen (\d+) 게시\(")
SKIPPED = re.compile(SITE_ID + r" gen (\d+)은 이미 게시됨 — 건너뜀")
CURRENT = re.compile(r"게시본이 최신인 현장\(변경 없음\): ([^\r\n]+)")
SUMMARY_KEYS: tuple[str, ...] = ("published", "skipped", "current", "failed", "violations")


@dataclass(frozen=True)
class PublishOutcome:
    """publish run 로그에서 읽은 결과. 요약 줄이 없으면 `counts`가 None(못 읽음 — 판정은 거짓)."""

    counts: dict[str, int] | None
    published: dict[str, int]
    """게시한 현장 → gen."""
    skipped: dict[str, int]
    current: frozenset[str]
    e_approval: bool
    """로그에 `E_APPROVAL`이 있다."""


def publish_outcome(log: str) -> PublishOutcome:
    """publish run 로그(`gh run view --log`) 해석 — `publish.py`의 요약 줄(`publish: 게시 N, …`),
    현장별 게시 알림(`<site> gen <n> 게시(`), 건너뜀 경고, 최신 현장 알림, `E_APPROVAL`."""
    found = SUMMARY.findall(log)
    counts = dict(zip(SUMMARY_KEYS, map(int, found[-1]), strict=True)) if found else None
    current: set[str] = set()
    for line in CURRENT.findall(log):
        current |= {name.strip() for name in line.split(",") if name.strip()}
    return PublishOutcome(
        counts=counts,
        published={site: int(gen) for site, gen in PUBLISHED.findall(log)},
        skipped={site: int(gen) for site, gen in SKIPPED.findall(log)},
        current=frozenset(current),
        e_approval="E_APPROVAL" in log,
    )


def bot_title(run_id: str, what: str) -> str:
    return f"{BOT_TITLE} {run_id}] {what}"


def bot_branch(run_id: str, what: str) -> str:
    return f"{BOT_BRANCH}{run_id}-{what}"


# ---------------------------------------------------------------- repo 조작


@dataclass(frozen=True)
class Pull:
    number: int
    head: str
    """PR 머리 커밋 sha(만든 직후 값 — merge 직전에는 `Repo.pull`로 다시 읽는다)."""
    title: str


@dataclass(frozen=True)
class Run:
    """workflow run 하나(`actions/runs`)."""

    id: int
    status: str
    conclusion: str | None
    head_sha: str
    event: str


def _run_of(raw: Mapping[str, object]) -> Run:
    conclusion = raw.get("conclusion")
    return Run(
        id=int(str(raw.get("id"))),
        status=str(raw.get("status")),
        conclusion=None if conclusion is None else str(conclusion),
        head_sha=str(raw.get("head_sha")),
        event=str(raw.get("event")),
    )


@dataclass
class Repo:
    """GitHub repo 하나(`owner/name`)에 대한 조작. 쓰기는 `author`(PR·브랜치·파일)와
    `approver`(승인·merge·ref 강제 갱신·secret)를 가려 쓴다. 읽기는 `approver`."""

    gh: Gh
    name: str
    author: str = AUTHOR
    approver: str = APPROVER
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _probes: set[int] = field(default_factory=set[int])
    """`open_probe`로 연 PR 번호 — `merge_unapproved`가 받는 유일한 PR."""

    def _get(self, path: str) -> object:
        return self.gh.api(self.approver, "GET", f"repos/{self.name}/{path}")

    # ---- 읽기

    def ref_sha(self, branch: str) -> str | None:
        try:
            ref = cast(dict[str, object], self._get(f"git/ref/heads/{branch}"))
        except NotFoundError:
            return None
        return str(cast(dict[str, object], ref.get("object") or {}).get("sha") or "") or None

    def main_sha(self) -> str:
        sha = self.ref_sha(MAIN)
        if sha is None:
            raise FleetError(f"{self.name}: no {MAIN} branch")
        return sha

    def read_file(self, path: str, ref: str) -> bytes | None:
        try:
            found = self._get(f"contents/{path}?ref={ref}")
        except NotFoundError:
            return None
        entry = cast(dict[str, object], found) if isinstance(found, dict) else {}
        return base64.b64decode(str(entry.get("content") or ""))

    def _file_sha(self, path: str, ref: str) -> str | None:
        try:
            found = self._get(f"contents/{path}?ref={ref}")
        except NotFoundError:
            return None
        return str(cast(dict[str, object], found).get("sha") or "") or None

    def open_pulls(self) -> list[dict[str, object]]:
        found = self._get("pulls?state=open&per_page=100")
        return [cast(dict[str, object], p) for p in cast(list[object], found or [])]

    def pull(self, number: int) -> dict[str, object]:
        return cast(dict[str, object], self._get(f"pulls/{number}"))

    def reviews(self, number: int) -> list[dict[str, object]]:
        found = self._get(f"pulls/{number}/reviews?per_page=100")
        return [cast(dict[str, object], r) for r in cast(list[object], found or [])]

    def pulls_of_commit(self, sha: str) -> list[dict[str, object]]:
        found = self._get(f"commits/{sha}/pulls")
        return [cast(dict[str, object], p) for p in cast(list[object], found or [])]

    def branches(self, prefix: str) -> list[str]:
        found = self._get(f"git/matching-refs/heads/{prefix}")
        refs = [cast(dict[str, object], r) for r in cast(list[object], found or [])]
        return [str(r.get("ref")).removeprefix("refs/heads/") for r in refs]

    def tag_sha(self, tag: str) -> str | None:
        try:
            ref = cast(dict[str, object], self._get(f"git/ref/tags/{tag}"))
        except NotFoundError:
            return None
        return str(cast(dict[str, object], ref.get("object") or {}).get("sha") or "") or None

    def commit_tree(self, sha: str) -> str:
        commit = cast(dict[str, object], self._get(f"git/commits/{sha}"))
        return str(cast(dict[str, object], commit.get("tree") or {}).get("sha"))

    # ---- 쓰기(작성자)

    def create_branch(self, branch: str, sha: str) -> None:
        body = {"ref": f"refs/heads/{branch}", "sha": sha}
        self.gh.api(self.author, "POST", f"repos/{self.name}/git/refs", body)

    def put_file(self, branch: str, path: str, content: bytes, message: str) -> str:
        """`branch`의 `path`를 `content`로(없으면 새로). 새 커밋 sha."""
        body: dict[str, object] = {
            "message": message,
            "branch": branch,
            "content": base64.b64encode(content).decode("ascii"),
        }
        old = self._file_sha(path, branch)
        if old is not None:
            body["sha"] = old
        done = cast(
            dict[str, object],
            self.gh.api(self.author, "PUT", f"repos/{self.name}/contents/{path}", body),
        )
        return str(cast(dict[str, object], done.get("commit") or {}).get("sha"))

    def open_pull(self, branch: str, title: str, body: str) -> Pull:
        payload = {"title": title, "head": branch, "base": MAIN, "body": body}
        done = cast(
            dict[str, object], self.gh.api(self.author, "POST", f"repos/{self.name}/pulls", payload)
        )
        head = str(cast(dict[str, object], done.get("head") or {}).get("sha"))
        return Pull(int(str(done.get("number"))), head, title)

    def open_probe(self, branch: str, title: str, body: str) -> Pull:
        """`E_APPROVAL` 시험의 PR — 이 번호만 `merge_unapproved`가 받는다."""
        pull = self.open_pull(branch, title, body)
        self._probes.add(pull.number)
        return pull

    # ---- 쓰기(승인자)

    def approve(self, number: int, head: str, body: str) -> None:
        payload = {"event": "APPROVE", "commit_id": head, "body": body}
        self.gh.api(self.approver, "POST", f"repos/{self.name}/pulls/{number}/reviews", payload)

    def _squash(self, number: int, head: str) -> str:
        payload = {"merge_method": "squash", "sha": head}
        done = cast(
            dict[str, object],
            self.gh.api(self.approver, "PUT", f"repos/{self.name}/pulls/{number}/merge", payload),
        )
        sha = str(done.get("sha") or "")
        if not sha:
            raise FleetError(f"{self.name} PR #{number}: merge returned no sha")
        return sha

    def merge(self, number: int) -> str:
        """승인된 PR만 squash merge한다: 지금 head sha를 다시 읽고 `pulls/N/reviews`에 그 head의
        승인(`approved_at_head`)이 있을 때만. 없으면 merge하지 않고 `FleetError`."""
        head = str(cast(dict[str, object], self.pull(number).get("head") or {}).get("sha") or "")
        if not approved_at_head(self.reviews(number), head, self.approver):
            raise FleetError(
                f"{self.name} PR #{number}: no {self.approver} approval at head {head[:12]} — "
                "not merged"
            )
        return self._squash(number, head)

    def merge_unapproved(self, pull: Pull) -> str:
        """`E_APPROVAL` 시험 전용: `open_probe`로 연 PR을 승인 없이 merge한다. 다른 PR이거나 그 PR에
        승인이 있으면 멈춘다(승인된 merge는 `merge`로만)."""
        if pull.number not in self._probes:
            raise FleetError(f"PR #{pull.number} is not an E_APPROVAL probe — use merge()")
        head = str(
            cast(dict[str, object], self.pull(pull.number).get("head") or {}).get("sha") or ""
        )
        if any(r.get("state") == "APPROVED" for r in self.reviews(pull.number)):
            raise FleetError(f"probe PR #{pull.number} has an approval — not an E_APPROVAL probe")
        return self._squash(pull.number, head)

    def close_pull(self, number: int) -> None:
        self.gh.api(
            self.approver, "PATCH", f"repos/{self.name}/pulls/{number}", {"state": "closed"}
        )

    def delete_branch(self, branch: str) -> None:
        try:
            self.gh.api(self.approver, "DELETE", f"repos/{self.name}/git/refs/heads/{branch}")
        except NotFoundError:
            return

    def force_branch(self, branch: str, sha: str) -> None:
        """`branch`를 `sha`로 강제 갱신한다(없으면 만든다)."""
        try:
            self.gh.api(
                self.approver,
                "PATCH",
                f"repos/{self.name}/git/refs/heads/{branch}",
                {"sha": sha, "force": True},
            )
        except FleetError as exc:
            if "(HTTP 404)" not in str(exc) and "(HTTP 422)" not in str(exc):
                raise
            body = {"ref": f"refs/heads/{branch}", "sha": sha}
            self.gh.api(self.approver, "POST", f"repos/{self.name}/git/refs", body)

    def create_tag(self, tag: str, sha: str) -> None:
        body = {"ref": f"refs/tags/{tag}", "sha": sha}
        self.gh.api(self.approver, "POST", f"repos/{self.name}/git/refs", body)

    def commit_files(
        self,
        parent: str,
        files: Mapping[str, bytes],
        message: str,
        *,
        replace: bool = False,
        account: str | None = None,
    ) -> str:
        """`parent`를 부모로 `files`(경로 → 내용)를 담은 커밋을 만든다(ref는 움직이지 않는다).
        기본은 부모 트리 위에 얹고, `replace`면 트리가 `files`만이다. 새 커밋 sha."""
        who = account or self.approver
        base = None if replace else self.commit_tree(parent)
        entries: list[dict[str, object]] = []
        for path, content in sorted(files.items()):
            blob = cast(
                dict[str, object],
                self.gh.api(
                    who,
                    "POST",
                    f"repos/{self.name}/git/blobs",
                    {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
                ),
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = cast(
            dict[str, object],
            self.gh.api(
                who,
                "POST",
                f"repos/{self.name}/git/trees",
                {"tree": entries} if base is None else {"base_tree": base, "tree": entries},
            ),
        )
        commit = cast(
            dict[str, object],
            self.gh.api(
                who,
                "POST",
                f"repos/{self.name}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [parent]},
            ),
        )
        return str(commit["sha"])

    def set_secret(self, secret: str, value: str) -> None:
        """repo secret — 값은 stdin으로만(끝 개행 없이: PowerShell 파이프가 붙이는 개행이 값에
        들어간 일이 있었다, M4-2 리허설 §3-2)."""
        self.gh.call(self.approver, ["secret", "set", secret, "-R", self.name], stdin=value)

    # ---- 대기

    def _until[T](self, what: str, timeout_s: float, probe: Callable[[], T | None]) -> T:
        deadline = self.clock() + timeout_s
        while True:
            got = probe()
            if got is not None:
                return got
            if self.clock() >= deadline:
                raise FleetError(f"{self.name}: {what} not seen within {timeout_s:.0f}s")
            self.sleep(POLL_S)

    def wait_check(self, number: int, timeout_s: float = CHECK_TIMEOUT_S) -> str:
        """PR 머리의 check-gen이 끝날 때까지(머리는 매번 다시 읽는다). 결론(`success` 등)."""

        def probe() -> str | None:
            head = str(cast(dict[str, object], self.pull(number).get("head") or {}).get("sha"))
            found = cast(dict[str, object], self._get(f"commits/{head}/check-runs?per_page=100"))
            runs = [
                cast(dict[str, object], r)
                for r in cast(list[object], found.get("check_runs") or [])
            ]
            mine = [r for r in runs if CHECK_GEN in str(r.get("name"))]
            if mine and all(r.get("status") == "completed" for r in mine):
                bad = [r for r in mine if r.get("conclusion") != "success"]
                return "success" if not bad else str(bad[0].get("conclusion"))
            return None

        return self._until(f"PR #{number} {CHECK_GEN}", timeout_s, probe)

    def runs(self, workflow: str, head_sha: str) -> list[Run]:
        found = cast(
            dict[str, object],
            self._get(f"actions/workflows/{workflow}/runs?head_sha={head_sha}&per_page=50"),
        )
        return [
            _run_of(cast(dict[str, object], r))
            for r in cast(list[object], found.get("workflow_runs") or [])
        ]

    def wait_new_run(
        self, workflow: str, head_sha: str, known: set[int], timeout_s: float = RUN_APPEAR_TIMEOUT_S
    ) -> Run:
        """`head_sha`의 `workflow` run 중 `known`(push 전에 이미 있던 id)에 없는 것이 생길
        때까지."""

        def probe() -> Run | None:
            fresh = [r for r in self.runs(workflow, head_sha) if r.id not in known]
            return min(fresh, key=lambda r: r.id) if fresh else None

        return self._until(f"{workflow} run for {head_sha[:12]}", timeout_s, probe)

    def wait_run(self, run: Run, timeout_s: float = RUN_TIMEOUT_S) -> Run:
        def probe() -> Run | None:
            now = _run_of(cast(dict[str, object], self._get(f"actions/runs/{run.id}")))
            return now if now.status == "completed" else None

        return self._until(f"run {run.id} completion", timeout_s, probe)

    def run_log(self, run: Run) -> str:
        return self.gh.call(
            self.approver,
            ["run", "view", str(run.id), "-R", self.name, "--log"],
            timeout_s=LOG_TIMEOUT_S,
        )


def iso(moment: datetime) -> str:
    """fleet 현장 파일·GitHub 시각 표기(`2026-11-14T03:19:36Z`, UTC)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
