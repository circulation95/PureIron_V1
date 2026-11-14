"""nightly의 GitHub 호출(M6-2 E9) — `e2e-history` 이력, commit status, 이슈, 임시 브랜치.

`GhCli`는 `gh api`로 부른다(토큰은 gh가 `GH_TOKEN`·자기 인증에서 읽는다 — 명령 줄·파일·로그에
남기지 않는다, 요청 본문은 stdin). `DryRun`은 쓰기를 하지 않고 무엇을 보낼지만 출력한다(읽기는
주어진 reader로, 없으면 빈 값). 시험은 같은 Protocol을 만족하는 작은 가짜를 쓴다.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime
from typing import Protocol, cast

GH_TIMEOUT_S = 60
HISTORY_BRANCH = "e2e-history"
HISTORY_FILE = "history.jsonl"
"""M6-3b 전 이력 파일(리허설·3a 줄) — 더 쓰지 않고 읽기만 한다(`publish.history_reads`)."""
TEMP_BRANCH_PREFIX = "e2e-tmp/"
"""M6-4 fleet 경로가 만드는 임시 브랜치 접두 — preflight가 7일 넘은 것을 지운다."""


class GitHubError(RuntimeError):
    pass


class GitHub(Protocol):
    def read_file(self, branch: str, path: str) -> str | None: ...

    def append_file(self, branch: str, path: str, text: str, message: str) -> None: ...

    def set_status(self, sha: str, state: str, context: str, description: str) -> None: ...

    def open_issue(self, label: str) -> int | None: ...

    def create_issue(self, title: str, body: str, label: str) -> None: ...

    def comment_issue(self, number: int, body: str) -> None: ...

    def temp_branches(self, prefix: str) -> list[tuple[str, datetime]]: ...

    def delete_branch(self, name: str) -> None: ...


class GhCli:
    """`gh api` 구현. `repo`는 `owner/name`(Actions에서는 `GITHUB_REPOSITORY`)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    @classmethod
    def from_env(cls) -> GhCli:
        repo = os.environ.get("GITHUB_REPOSITORY") or cls._gh(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        )
        return cls(repo.strip())

    @staticmethod
    def _gh(*args: str, body: object | None = None) -> str:
        try:
            done = subprocess.run(
                ["gh", *args],
                input=None if body is None else json.dumps(body),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=GH_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"gh {args[0]} failed: {exc}") from exc
        if done.returncode != 0:
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {done.stderr.strip()[-300:]}")
        return done.stdout

    def _api(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        args = ["api", "-X", method, f"repos/{self.repo}/{path}"]
        if body is not None:
            args += ["--input", "-"]
        out = self._gh(*args, body=body)
        return json.loads(out) if out.strip() else None

    def _exists(self, path: str) -> object | None:
        try:
            return self._api(path)
        except GitHubError as exc:
            if "404" in str(exc):
                return None
            raise

    def read_file(self, branch: str, path: str) -> str | None:
        found = self._exists(f"contents/{path}?ref={branch}")
        if not isinstance(found, dict):
            return None
        content = str(cast(dict[str, object], found).get("content") or "")
        return base64.b64decode(content).decode("utf-8")

    def append_file(self, branch: str, path: str, text: str, message: str) -> None:
        """`branch`가 없으면 빈 뿌리(부모 없는 커밋)로 만든다."""
        if self._exists(f"branches/{branch}") is None:
            self._orphan(branch, path, text, message)
            return
        found = self._exists(f"contents/{path}?ref={branch}")
        old, sha = "", None
        if isinstance(found, dict):
            entry = cast(dict[str, object], found)
            old = base64.b64decode(str(entry.get("content") or "")).decode("utf-8")
            sha = entry.get("sha")
        body: dict[str, object] = {
            "message": message,
            "branch": branch,
            "content": base64.b64encode((old + text).encode("utf-8")).decode("ascii"),
        }
        if sha is not None:
            body["sha"] = sha
        self._api(f"contents/{path}", method="PUT", body=body)

    def _orphan(self, branch: str, path: str, text: str, message: str) -> None:
        blob = cast(
            dict[str, object],
            self._api("git/blobs", method="POST", body={"content": text, "encoding": "utf-8"}),
        )
        tree = cast(
            dict[str, object],
            self._api(
                "git/trees",
                method="POST",
                body={
                    "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]
                },
            ),
        )
        commit = cast(
            dict[str, object],
            self._api(
                "git/commits",
                method="POST",
                body={"message": message, "tree": tree["sha"], "parents": []},
            ),
        )
        self._api(
            "git/refs", method="POST", body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]}
        )

    def set_status(self, sha: str, state: str, context: str, description: str) -> None:
        body = {"state": state, "context": context, "description": description}
        self._api(f"statuses/{sha}", method="POST", body=body)

    def open_issue(self, label: str) -> int | None:
        found = self._api(f"issues?state=open&labels={label}&per_page=1")
        items = cast(list[dict[str, object]], found) if isinstance(found, list) else []
        return int(str(items[0]["number"])) if items else None

    def create_issue(self, title: str, body: str, label: str) -> None:
        self._api("issues", method="POST", body={"title": title, "body": body, "labels": [label]})

    def comment_issue(self, number: int, body: str) -> None:
        self._api(f"issues/{number}/comments", method="POST", body={"body": body})

    def temp_branches(self, prefix: str) -> list[tuple[str, datetime]]:
        refs = self._api(f"git/matching-refs/heads/{prefix}")
        found: list[tuple[str, datetime]] = []
        for ref in cast(list[dict[str, object]], refs) if isinstance(refs, list) else []:
            name = str(ref.get("ref")).removeprefix("refs/heads/")
            target = cast(dict[str, object], ref.get("object") or {})
            commit = cast(dict[str, object], self._api(f"commits/{target.get('sha')}"))
            info = cast(dict[str, dict[str, object]], commit.get("commit") or {})
            stamp = str(info.get("committer", {}).get("date") or "")
            found.append((name, datetime.fromisoformat(stamp.replace("Z", "+00:00"))))
        return found

    def delete_branch(self, name: str) -> None:
        self._api(f"git/refs/heads/{name}", method="DELETE")


class DryRun:
    """쓰기 대신 무엇을 보낼지 출력한다(`--dry-run`). 읽기는 `reader`로 하고 실패하면 빈 값이다."""

    def __init__(self, reader: GitHub | None = None) -> None:
        self.reader = reader
        self.writes: list[dict[str, object]] = []

    def _write(self, action: str, **detail: object) -> None:
        entry: dict[str, object] = {"dry_run": action, **detail}
        self.writes.append(entry)
        print(json.dumps(entry, ensure_ascii=False, sort_keys=True), flush=True)

    def read_file(self, branch: str, path: str) -> str | None:
        if self.reader is None:
            return None
        try:
            return self.reader.read_file(branch, path)
        except GitHubError as exc:
            print(f"dry-run: cannot read {branch}:{path} ({exc})", flush=True)
            return None

    def append_file(self, branch: str, path: str, text: str, message: str) -> None:
        self._write("append_file", branch=branch, path=path, message=message, text=text)

    def set_status(self, sha: str, state: str, context: str, description: str) -> None:
        self._write("set_status", sha=sha, state=state, context=context, description=description)

    def open_issue(self, label: str) -> int | None:
        if self.reader is None:
            return None
        try:
            return self.reader.open_issue(label)
        except GitHubError:
            return None

    def create_issue(self, title: str, body: str, label: str) -> None:
        self._write("create_issue", title=title, label=label, body=body)

    def comment_issue(self, number: int, body: str) -> None:
        self._write("comment_issue", number=number, body=body)

    def temp_branches(self, prefix: str) -> list[tuple[str, datetime]]:
        if self.reader is None:
            return []
        try:
            return self.reader.temp_branches(prefix)
        except GitHubError:
            return []

    def delete_branch(self, name: str) -> None:
        self._write("delete_branch", name=name)
