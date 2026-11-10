"""G2 flake 지표와 격리 목록(M6-2, 상위 계획 PM-3).

- flake 비율: 최근 `WINDOW_DAYS`일 이력에서 같은 `product_sha`에 통과한 실행과 실패한
  실행이 모두 있는 실행 수 / 전체 실행 수. 실행 하나는 시험이 하나라도 실패하면 실패다. 같은
  커밋이 통과도 실패도 했다면 그 실패는 제품이 아니라 흔들림이다 — 다른 커밋끼리는 비교하지
  않는다.
- `quarantine.yaml`: `{quarantine: [{test_id, issue, added}]}`. 항목은 이슈 링크가 있어야 하고
  `added`(`"YYYY-MM-DD"`, YAML 날짜가 아닌 문자열)부터 `QUARANTINE_DAYS`일이 지나면 만료돼 효력이
  없다(nightly가 summary에 만료 목록을 남긴다).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from harness.context import E2EError
from pureiron_project import canonical

WINDOW_DAYS = 7
QUARANTINE_DAYS = 7
ISSUE_PREFIX = "https://github.com/"


def flake_rate(
    history: Iterable[Mapping[str, object]], today: date, window_days: int = WINDOW_DAYS
) -> float:
    since = today - timedelta(days=window_days)
    runs: dict[str, tuple[str, bool]] = {}
    for row in history:
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        if not since < day <= today:
            continue
        run, sha = str(row.get("run")), str(row.get("product_sha"))
        failed = row.get("outcome") == "fail"
        runs[run] = (sha, runs.get(run, (sha, False))[1] or failed)
    outcomes: dict[str, set[bool]] = {}
    for sha, failed in runs.values():
        outcomes.setdefault(sha, set()).add(failed)
    flaky = sum(1 for sha, _ in runs.values() if outcomes[sha] == {True, False})
    return round(flaky / len(runs), 3) if runs else 0.0


@dataclass(frozen=True)
class Quarantine:
    test_id: str
    issue: str
    added: date

    def expired(self, today: date) -> bool:
        return today > self.added + timedelta(days=QUARANTINE_DAYS)


def parse_quarantine(text: str) -> list[Quarantine]:
    doc = canonical.load(text, "quarantine.yaml")
    items = doc.get("quarantine") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise E2EError("quarantine.yaml: top-level 'quarantine' list missing")
    entries: list[Quarantine] = []
    for item in items:
        if not isinstance(item, dict):
            raise E2EError(f"quarantine.yaml: entry {item!r} is not a mapping")
        test_id, issue, added = item.get("test_id"), item.get("issue"), item.get("added")
        if not isinstance(test_id, str) or not test_id:
            raise E2EError(f"quarantine.yaml: entry {item!r} has no test_id")
        if not isinstance(issue, str) or not issue.startswith(ISSUE_PREFIX):
            raise E2EError(f"quarantine.yaml: {test_id} needs an issue link ({ISSUE_PREFIX}...)")
        try:
            day = date.fromisoformat(str(added))
        except ValueError as exc:
            raise E2EError(f"quarantine.yaml: {test_id} added {added!r} is not YYYY-MM-DD") from exc
        entries.append(Quarantine(test_id, issue, day))
    return entries


def load_quarantine(path: Path) -> list[Quarantine]:
    return parse_quarantine(path.read_text(encoding="utf-8")) if path.is_file() else []


def split_expired(
    entries: Iterable[Quarantine], today: date
) -> tuple[frozenset[str], list[Quarantine]]:
    """(효력 있는 test_id, 만료된 항목)."""
    listed = list(entries)
    active = frozenset(e.test_id for e in listed if not e.expired(today))
    return active, [e for e in listed if e.expired(today)]
