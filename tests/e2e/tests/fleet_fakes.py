"""fleet 단위 시험이 같이 쓰는 가짜(M6-4a) — 메모리 안의 GitHub(`gh` 명령 단위)와 fleet 세션 원격
호출.

시험 파일이 아니다(수집되지 않는다). `FakeGitHub`는 `fleet.Gh`의 `run` 자리에 들어가 `gh auth
token`· `gh api`·`gh run view --log`·`gh secret set`을 흉내 내고, 받은 호출(인자·stdin·환경)을 모두
`calls`에 남긴다 — 시험이 토큰이 인자·출력·예외에 없는지 본다.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness import baseline, fleet, fleet_session, g2
from harness.context import Context
from harness.fleet import Gh, Repo
from harness.vm import Vm

TOKENS = {"ws-blip": "gho_AUTHORSECRET0001", "circulation95": "gho_OWNERSECRET0002"}
SUMMARY_OK = "publish: 게시 1, 건너뜀 0, 최신 1, 실패 0, 규칙 위반 0"


def done(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str]([], rc, stdout, stderr)


@dataclass(frozen=True)
class Req:
    account: str
    repo: str
    m: re.Match[str]
    q: dict[str, str]
    body: dict[str, object]


@dataclass
class Call:
    args: list[str]
    stdin: str | None
    env: dict[str, str]


Publish = Callable[[str, str, str], tuple[str, str]]
"""(repo, sha, event) → (결론, 로그) — push가 띄우는 publish run."""


def default_publish(repo: str, sha: str, event: str) -> tuple[str, str]:
    return "success", f"::notice::site-a gen 2 게시(x.pib, 1 bytes)\n{SUMMARY_OK}\n"


@dataclass
class FakeGitHub:
    publish: Publish = default_publish
    check_conclusion: str = "success"
    calls: list[Call] = field(default_factory=list[Call])
    commits: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    refs: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    pulls: dict[tuple[str, int], dict[str, object]] = field(
        default_factory=dict[tuple[str, int], dict[str, object]]
    )
    reviews: dict[tuple[str, int], list[dict[str, object]]] = field(
        default_factory=dict[tuple[str, int], list[dict[str, object]]]
    )
    runs: dict[int, dict[str, object]] = field(default_factory=dict[int, dict[str, object]])
    logs: dict[int, str] = field(default_factory=dict[int, str])
    secrets: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    trees: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])
    counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    fail: dict[str, str] = field(default_factory=dict[str, str])
    """경로 조각 → stderr(그 호출을 실패시킨다)."""

    # ---- 상태 준비

    def sha(self) -> str:
        return f"{next(self.counter):040x}"

    def commit(self, files: Mapping[str, bytes], parents: Sequence[str] = ()) -> str:
        sha = self.sha()
        self.commits[sha] = {"files": dict(files), "parents": list(parents)}
        return sha

    def seed(self, repo: str, files: Mapping[str, bytes]) -> str:
        sha = self.commit(files)
        self.refs[(repo, "heads/main")] = sha
        return sha

    def files_at(self, repo: str, ref: str) -> dict[str, bytes]:
        sha = self.refs.get((repo, f"heads/{ref}"), ref)
        return cast(dict[str, bytes], self.commits.get(sha, {}).get("files", {}))

    def add_run(self, repo: str, sha: str, event: str) -> int:
        number = next(self.counter)
        conclusion, log = self.publish(repo, sha, event)
        self.runs[number] = {
            "id": number,
            "repo": repo,
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": sha,
            "event": event,
        }
        self.logs[number] = log
        return number

    # ---- gh

    def __call__(
        self, args: Sequence[str], stdin: str | None, env: Mapping[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(Call(list(args), stdin, dict(env)))
        if list(args[:2]) == ["auth", "token"]:
            return done(TOKENS[args[3]] + "\n")
        token = env.get("GH_TOKEN", "")
        account = next((a for a, t in TOKENS.items() if t == token), "")
        if not account:
            return done(rc=4, stderr="gh: To use GitHub CLI, set GH_TOKEN")
        if args[0] != "api":
            return self.other(args, stdin)
        method, path = args[2], args[3]
        failing = [e for piece, e in self.fail.items() if piece in f"{method} {path}"]
        if failing:
            return done(rc=1, stderr=failing[0])
        body = cast(dict[str, object], json.loads(stdin)) if stdin else {}
        try:
            out = self.api(account, method, path, body)
        except KeyError:
            return done(rc=1, stderr="gh: Not Found (HTTP 404)")
        return done("" if out is None else json.dumps(out))

    def other(self, args: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        """`gh secret set NAME -R REPO`(값은 stdin)·`gh run view ID -R REPO --log`."""
        if args[0] == "secret":
            self.secrets[(args[4], args[2])] = stdin or ""
            return done()
        return done(self.logs[int(args[2])])

    def api(self, account: str, method: str, path: str, body: dict[str, object]) -> object:
        m = re.fullmatch(r"repos/([^/]+/[^/]+)/([^?]*)\??(.*)", path)
        assert m is not None, path
        repo, rest, query = m[1], m[2], m[3]
        q = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        for verb, pattern, handler in self.routes():
            found = re.fullmatch(pattern, rest)
            if verb == method and found is not None:
                return handler(Req(account, repo, found, q, body))
        raise AssertionError(f"unexpected gh api {method} {path}")

    def routes(self) -> list[tuple[str, str, Callable[[Req], object]]]:
        return [
            ("GET", r"git/ref/(heads|tags)/(.+)", self.get_ref),
            ("GET", r"git/matching-refs/heads/(.*)", self.matching),
            ("POST", r"git/refs", self.post_ref),
            ("PATCH", r"git/refs/(heads/.+)", self.patch_ref),
            ("DELETE", r"git/refs/(heads/.+)", self.delete_ref),
            ("GET", r"contents/(.+)", self.get_contents),
            ("PUT", r"contents/(.+)", self.put_contents),
            ("GET", r"pulls", self.open_pulls),
            ("POST", r"pulls", self.post_pull),
            ("GET", r"pulls/(\d+)", lambda r: self.pulls[(r.repo, int(r.m[1]))]),
            ("PATCH", r"pulls/(\d+)", self.patch_pull),
            ("GET", r"pulls/(\d+)/reviews", lambda r: self.reviews[(r.repo, int(r.m[1]))]),
            ("POST", r"pulls/(\d+)/reviews", self.post_review),
            ("PUT", r"pulls/(\d+)/merge", self.merge),
            ("GET", r"commits/([0-9a-f]+)/check-runs", self.check_runs),
            ("GET", r"commits/([0-9a-f]+)/pulls", self.commit_pulls),
            ("GET", r"actions/workflows/[^/]+/runs", self.workflow_runs),
            ("GET", r"actions/runs/(\d+)", lambda r: self.runs[int(r.m[1])]),
            ("GET", r"git/commits/([0-9a-f]+)", self.git_commit),
            ("POST", r"git/blobs", self.post_blob),
            ("POST", r"git/trees", self.post_tree),
            ("POST", r"git/commits", self.post_commit),
        ]

    def get_ref(self, r: Req) -> object:
        return {"object": {"sha": self.refs[(r.repo, f"{r.m[1]}/{r.m[2]}")]}}

    def matching(self, r: Req) -> object:
        prefix = f"heads/{r.m[1]}"
        names = [n for (rp, n) in self.refs if rp == r.repo and n.startswith(prefix)]
        return [{"ref": f"refs/{n}"} for n in names]

    def post_ref(self, r: Req) -> object:
        self.refs[(r.repo, str(r.body["ref"]).removeprefix("refs/"))] = str(r.body["sha"])
        return {"ref": r.body["ref"]}

    def patch_ref(self, r: Req) -> object:
        if (r.repo, r.m[1]) not in self.refs:
            raise KeyError(r.m[1])
        self.refs[(r.repo, r.m[1])] = str(r.body["sha"])
        if r.m[1] == "heads/main":
            self.add_run(r.repo, str(r.body["sha"]), "push")
        return {"ref": r.m[1]}

    def delete_ref(self, r: Req) -> object:
        del self.refs[(r.repo, r.m[1])]
        return None

    def get_contents(self, r: Req) -> object:
        content = self.files_at(r.repo, r.q["ref"])[r.m[1]]
        return {"content": base64.b64encode(content).decode(), "sha": "blob"}

    def put_contents(self, r: Req) -> object:
        branch = f"heads/{r.body['branch']}"
        head = self.refs[(r.repo, branch)]
        files = {**self.files_at(r.repo, head), r.m[1]: base64.b64decode(str(r.body["content"]))}
        sha = self.commit(files, [head])
        self.refs[(r.repo, branch)] = sha
        return {"commit": {"sha": sha}}

    def open_pulls(self, r: Req) -> object:
        return [p for (rp, _), p in self.pulls.items() if rp == r.repo and p["state"] == "open"]

    def post_pull(self, r: Req) -> object:
        number = len([1 for (rp, _) in self.pulls if rp == r.repo]) + 1
        head = self.refs[(r.repo, f"heads/{r.body['head']}")]
        self.pulls[(r.repo, number)] = {
            "number": number,
            "title": r.body["title"],
            "head": {"sha": head, "ref": r.body["head"]},
            "state": "open",
            "merged_at": None,
            "merge_commit_sha": None,
            "user": {"login": r.account},
        }
        self.reviews[(r.repo, number)] = []
        return self.pulls[(r.repo, number)]

    def patch_pull(self, r: Req) -> object:
        data = self.pulls[(r.repo, int(r.m[1]))]
        data["state"] = str(r.body["state"])
        return data

    def post_review(self, r: Req) -> object:
        review = {"user": {"login": r.account}, "state": "APPROVED"}
        self.reviews[(r.repo, int(r.m[1]))].append({**review, "commit_id": r.body["commit_id"]})
        return review

    def merge(self, r: Req) -> object:
        data = self.pulls[(r.repo, int(r.m[1]))]
        head = str(cast(dict[str, object], data["head"])["sha"])
        if r.body.get("sha") != head:
            raise KeyError("head moved")
        sha = self.commit(self.files_at(r.repo, head), [self.refs[(r.repo, "heads/main")]])
        self.refs[(r.repo, "heads/main")] = sha
        data |= {"state": "closed", "merged_at": "2026-11-15T00:00:00Z", "merge_commit_sha": sha}
        self.add_run(r.repo, sha, "push")
        return {"sha": sha, "merged": True}

    def check_runs(self, r: Req) -> object:
        run = {"name": "check-gen / check-gen", "status": "completed"}
        return {"check_runs": [{**run, "conclusion": self.check_conclusion}]}

    def commit_pulls(self, r: Req) -> object:
        pulls = self.pulls.items()
        return [p for (rp, _), p in pulls if rp == r.repo and p["merge_commit_sha"] == r.m[1]]

    def workflow_runs(self, r: Req) -> object:
        runs = self.runs.values()
        mine = [x for x in runs if x["repo"] == r.repo and x["head_sha"] == r.q["head_sha"]]
        return {"workflow_runs": mine}

    def git_commit(self, r: Req) -> object:
        self.trees[f"tree-{r.m[1]}"] = cast(dict[str, bytes], self.commits[r.m[1]]["files"])
        return {"tree": {"sha": f"tree-{r.m[1]}"}}

    def post_blob(self, r: Req) -> object:
        sha = f"blob-{next(self.counter)}"
        self.blobs[sha] = base64.b64decode(str(r.body["content"]))
        return {"sha": sha}

    def post_tree(self, r: Req) -> object:
        base = dict(self.trees[str(r.body["base_tree"])]) if "base_tree" in r.body else {}
        for entry in cast(list[dict[str, object]], r.body["tree"]):
            base[str(entry["path"])] = self.blobs[str(entry["sha"])]
        sha = f"tree-new-{next(self.counter)}"
        self.trees[sha] = base
        return {"sha": sha}

    def post_commit(self, r: Req) -> object:
        return {
            "sha": self.commit(self.trees[str(r.body["tree"])], cast(list[str], r.body["parents"]))
        }

    # ---- 검사

    def argv_and_stdin(self) -> str:
        return "\n".join(" ".join(c.args) + "\n" + (c.stdin or "") for c in self.calls)

    def writes(self, method: str, piece: str) -> list[Call]:
        return [c for c in self.calls if c.args[:3] == ["api", "-X", method] and piece in c.args[3]]


def repo(gh: FakeGitHub, name: str = fleet.FLEET_REPO) -> Repo:
    """가짜 시계는 부를 때마다 10초씩 간다 — 생기지 않는 run을 기다리면 끝없이 돌지 않고 시간 초과가
    된다."""
    ticks = itertools.count(0, 10)
    return Repo(
        Gh(gh, environ={"PATH": "x"}), name, sleep=lambda s: None, clock=lambda: float(next(ticks))
    )


def repos(gh: FakeGitHub) -> fleet_session.Repos:
    return fleet_session.Repos(
        repo(gh, fleet.FLEET_REPO), repo(gh, fleet.PROJECT_REPO), repo(gh, fleet.V1_REPO)
    )


def site_yaml(site: str, gen: int) -> bytes:
    now = datetime(2026, 11, 15, tzinfo=UTC)
    return baseline.site_file(site, gen, "2026.11.1", "v1", (now, now))


# ---------------------------------------------------------------- fleet 세션 원격 가짜


@dataclass
class FakeRemote:
    """중앙(`central.py`)·root WSL(`publish_host.py`)·Hyper-V·VM 조작의 가짜 — 호출을 `events`에."""

    session: g2.Session
    events: list[str] = field(default_factory=list[str])
    chain: list[str | None] = field(default_factory=list[str | None])
    """`chain`이 차례로 돌려줄 `last_approved`(마지막 값을 계속)."""
    fleet_rows: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    restore_reply: dict[str, object] = field(default_factory=lambda: {"fleet_match": True})
    dump_fleet: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    applied: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    current: dict[str, int] = field(default_factory=dict[str, int])
    """수렴한 현장의 중앙 current gen(`/fleet` 행에 들어간다)."""
    now: datetime = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    tick: float = 0.0

    def central(self, *args: str) -> dict[str, object]:
        self.events.append("central " + " ".join(args[:1] if args[0] != "fleet" else args[:3]))
        command = args[0]
        if command == "up":
            return {"ca_sha256": "AB:CD"}
        if command == "dump":
            return {"name": args[2], "bytes": 10, "seconds": 1.0, "fleet": self.dump_fleet}
        if command == "restore":
            return {**self.restore_reply, "seconds": 2.0}
        if command == "fleet":
            site = args[2]
            row = {"site": site, "current": {"gen": self.current.get(site)}}
            return {"status": 200, "body": [{**row, **self.fleet_rows.get(site, {})}]}
        return {"ok": True}

    def wsl_root(self, script: str, stdin: str | None = None) -> str:
        command = script.split("publish_host.py ", 1)[1].split(maxsplit=1)[0]
        self.events.append(f"host {command}")
        if command == "chain":
            value = self.chain.pop(0) if len(self.chain) > 1 else (self.chain or [None])[0]
            return json.dumps({"last_approved": value})
        return json.dumps({"ok": True})

    def wsl(self, script: str, **_: object) -> str:
        self.events.append("wsl " + script.rsplit(maxsplit=1)[-1])
        return "viewer-token-SECRET\n"

    def remote(self) -> fleet_session.Remote:
        def restore_vm(run: g2.SiteRun, snapshot: str, counters: Mapping[str, int]) -> None:
            self.events.append(f"vm-restore {run.ctx.site.site_id} {snapshot}")
            run.ctx.nft_last = dict(counters)

        def restore_c0(run: g2.SiteRun) -> None:
            self.events.append(f"c0 {run.ctx.site.site_id}")

        def online(ctx: Context, host: Vm, ca: Path, sha: str) -> dict[str, object]:
            self.events.append(f"online {ctx.site.site_id}")
            return {"registered": ctx.site.site_id}

        def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
            site = next(s for s, r in self.session.sites.items() if r.ctx.site.ip == host.ip)
            self.events.append(f"converge {site} g{gen}")
            want, result = self.applied.get(site, (gen, "succeeded"))
            self.current[site] = want
            return {"updater": {"applied_gen": want, "last_result": result}}, 3.0

        def checkpoint(vm: str, name: str) -> None:
            self.events.append(f"checkpoint {vm} {name}")

        def mark(ctx: Context, host: Vm) -> None:
            self.events.append(f"mark {ctx.site.site_id}")
            ctx.nft_last = {"ext_out_v4": 11}

        def clock() -> float:
            self.tick += 1.0
            return self.tick

        return fleet_session.Remote(
            central=self.central,
            wsl=self.wsl,
            wsl_root=self.wsl_root,
            checkpoint=checkpoint,
            restore_vm=restore_vm,
            restore_c0=restore_c0,
            online=online,
            probe=lambda host: {},
            converge=converge,
            wait_ready=lambda host: {},
            mark=mark,
            now=lambda: self.now,
            sleep=lambda s: None,
            clock=clock,
        )
