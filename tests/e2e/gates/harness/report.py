"""nightly 결과 정리(M6-2 E8·E9) — junit → 시험별 결과·분류, 실행 판정, infra 재시도 대상, summary,
`e2e-history` 이력 줄, commit status 값, product 연속 실패 이슈 판단, 같은 시험의 infra 연속 실패
판단(안전망 — 재시도로도 낫지 않는 infra는 사람에게 알린다). 모두 순수 함수다.

실행 판정: 격리(quarantine)되지 않은 실패가 없으면 `passed`, 하나라도 `product`면
`product`, 실패가 모두 `infra`면 `infra`(그때만 실패한 시험을 1회 다시 돌린다). junit에 분류
속성이 없는 실패(수집 오류 등)는 `product`다. 시험 결과가 하나도 없으면(pytest가 죽음·시간 초과)
`infra`다.

시작하지 못한 실행(이력에 시험 id `preflight` 줄만 있는 run — preflight에서 멈춤)은 이슈 판단과
flake 비율에서 빠진다(`not_started`, M6-2b ★G2·L8). 그런 밤은 PC를 끄거나 앱을 켜 둔 밤과 같은
종류라 이력 줄·commit status `error`·exit 2만 남기고 이슈는 열지 않는다. 시험 id `runner`·`pytest`
줄은 시작한 뒤 깨진 실행이라 그대로 센다.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import cast

from harness.failures import INFRA, PRODUCT

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
CLASS_PROPERTY = "failure_class"
STATUS_CONTEXT = "g2/nightly"
STATUS_STATE = {PASSED: "success", PRODUCT: "failure", INFRA: "error"}
"""실행 판정 → commit status `state`(infra는 제품 판정이 아니라 `error`)."""
ISSUE_LABEL = "g2-nightly"
ISSUE_TITLE = "G2 nightly: product failure in 2 consecutive runs"
INFRA_ISSUE_LABEL = "g2-nightly-infra"
INFRA_ISSUE_TITLE = "G2 nightly: the same test failed as infra in 2 consecutive runs"
DESCRIPTION_CHARS = 140
PREFLIGHT_TEST = "preflight"
"""preflight에서 멈춘 실행의 시험 id(infra) — 시험을 시작하지 못했다."""


@dataclass(frozen=True)
class TestResult:
    test_id: str
    """`<파일>::<시험 이름>`(gates 디렉터리 기준) — 재실행 node id와 이력의 열쇠."""
    outcome: str
    failure_class: str | None
    seconds: float
    retried: bool = False


def parse_junit(text: str) -> list[TestResult]:
    """pytest junit(xunit1)의 testcase마다 결과. 실패·오류가 있으면 `failed`이고 분류는 속성
    `failure_class`(여럿이면 `product`가 이긴다, 없으면 `product`)."""
    results: list[TestResult] = []
    for case in ET.fromstring(text).iter("testcase"):
        test_id = f"{case.get('file') or case.get('classname') or ''}::{case.get('name') or ''}"
        broken = case.find("failure") is not None or case.find("error") is not None
        classes = {p.get("value") for p in case.iter("property") if p.get("name") == CLASS_PROPERTY}
        if not broken:
            outcome = SKIPPED if case.find("skipped") is not None else PASSED
            results.append(TestResult(test_id, outcome, None, float(case.get("time") or 0)))
            continue
        failure_class = INFRA if classes == {INFRA} else PRODUCT
        results.append(TestResult(test_id, FAILED, failure_class, float(case.get("time") or 0)))
    return results


def verdict(results: Iterable[TestResult], quarantined: frozenset[str] = frozenset()) -> str:
    listed = list(results)
    if not listed:
        return INFRA
    failures = [r for r in listed if r.outcome == FAILED and r.test_id not in quarantined]
    if not failures:
        return PASSED
    return PRODUCT if any(r.failure_class != INFRA for r in failures) else INFRA


def retry_targets(
    results: Iterable[TestResult], quarantined: frozenset[str] = frozenset()
) -> list[str]:
    """다시 돌릴 시험 — 실행 판정이 `infra`일 때의 실패한 시험만. `product`가 하나라도 있으면 빈
    목록이다(제품 실패는 다시 돌리지 않는다)."""
    listed = list(results)
    if verdict(listed, quarantined) != INFRA:
        return []
    return [r.test_id for r in listed if r.outcome == FAILED]


def merge_retry(first: Iterable[TestResult], retry: Iterable[TestResult]) -> list[TestResult]:
    """재실행한 시험은 재실행 결과(`retried=True`)로 바꾼다."""
    again = {r.test_id: replace(r, retried=True) for r in retry}
    return [again.get(r.test_id, r) for r in first]


def summary(
    *,
    run_id: str,
    date: str,
    product_sha: str,
    results: Iterable[TestResult],
    quarantined: frozenset[str] = frozenset(),
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    listed = list(results)
    return {
        "run": run_id,
        "date": date,
        "product_sha": product_sha,
        "verdict": verdict(listed, quarantined),
        "tests": [asdict(r) for r in listed],
        "quarantined": sorted(quarantined),
        **(extra or {}),
    }


def test_rows(summary_: Mapping[str, object]) -> list[dict[str, object]]:
    """summary의 `tests` 행(형식이 다르면 빈 목록)."""
    tests = summary_.get("tests")
    items = cast(list[object], tests) if isinstance(tests, list) else []
    return [cast(dict[str, object], r) for r in items if isinstance(r, dict)]


def history_lines(summary_: Mapping[str, object]) -> list[str]:
    """`e2e-history/history.jsonl`에 더할 줄 — 시험마다 `{date, run, product_sha, test_id, outcome,
    class, seconds}`(키 정렬 JSON 한 줄). outcome은 `pass`·`fail`·`skip`, class는 실패일 때만."""
    lines: list[str] = []
    for row in test_rows(summary_):
        outcome = str(row.get("outcome"))
        line = {
            "date": summary_.get("date"),
            "run": summary_.get("run"),
            "product_sha": summary_.get("product_sha"),
            "test_id": row.get("test_id"),
            "outcome": {PASSED: "pass", FAILED: "fail"}.get(outcome, "skip"),
            "class": row.get("failure_class") if outcome == FAILED else None,
            "seconds": row.get("seconds"),
        }
        lines.append(json.dumps(line, sort_keys=True, ensure_ascii=False))
    return lines


def parse_history(text: str) -> list[dict[str, object]]:
    """history.jsonl 줄(빈 줄·깨진 줄은 건너뛴다 — 이력 하나가 nightly를 멈추지 않게)."""
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(cast(dict[str, object], row))
    return rows


def not_started(rows: Iterable[Mapping[str, object]]) -> bool:
    """한 run의 이력 줄이 시험 id `PREFLIGHT_TEST` 줄뿐인가 — 시작하지 못한 실행."""
    return {str(row.get("test_id")) for row in rows} == {PREFLIGHT_TEST}


def by_run(history: Iterable[Mapping[str, object]]) -> dict[str, list[Mapping[str, object]]]:
    runs: dict[str, list[Mapping[str, object]]] = {}
    for row in history:
        runs.setdefault(str(row.get("run")), []).append(row)
    return runs


def last_two_started(
    history: Iterable[Mapping[str, object]],
) -> list[list[Mapping[str, object]]]:
    """시작한 실행 중 가장 최근 두 개(run id 순)의 줄 — 시작하지 못한 실행은 건너뛴다(product →
    preflight 멈춤 → product는 2연속이다). 가장 최근 실행이 시작하지 못했으면 빈 목록이다(그 밤은
    이슈를 열지도 댓글을 달지도 않는다). 두 개가 없어도 빈 목록."""
    runs = by_run(history)
    order = sorted(runs)
    if not order or not_started(runs[order[-1]]):
        return []
    started = [runs[r] for r in order if not not_started(runs[r])]
    return started[-2:] if len(started) >= 2 else []


def failed_tests(rows: Iterable[Mapping[str, object]], failure_class: str) -> set[str]:
    return {
        str(row.get("test_id"))
        for row in rows
        if row.get("outcome") == "fail" and row.get("class") == failure_class
    }


def needs_issue(history: Iterable[Mapping[str, object]]) -> bool:
    """시작한 실행 중 가장 최근 두 개(`last_two_started`)가 모두 product 실패를 냈는가(E9)."""
    latest = last_two_started(history)
    return len(latest) == 2 and all(failed_tests(rows, PRODUCT) for rows in latest)


def repeated_infra(history: Iterable[Mapping[str, object]]) -> list[str]:
    """시작한 실행 중 가장 최근 두 개(`last_two_started`) 모두에서 infra로 실패한 시험 id(정렬).
    infra는 재시도 뒤의 결과이므로, 여기 남는 것은 재시도로도 낫지 않은 실행 환경 문제이거나
    infra로 분류된 제품 문제(시간 초과로 드러난 멈춤, 원격 명령 종료 코드 255)다. preflight에서
    멈춘 밤은 세지 않는다(★G2)."""
    latest = last_two_started(history)
    if len(latest) != 2:
        return []
    return sorted(failed_tests(latest[0], INFRA) & failed_tests(latest[1], INFRA))


def status_description(summary_: Mapping[str, object]) -> str:
    rows = test_rows(summary_)
    failed = [str(r.get("test_id")).rsplit("::", 1)[-1] for r in rows if r.get("outcome") == FAILED]
    text = f"G2 nightly {summary_.get('verdict')}: {len(rows) - len(failed)}/{len(rows)} passed"
    if failed:
        text += " - failed " + ", ".join(failed)
    return text[:DESCRIPTION_CHARS]
