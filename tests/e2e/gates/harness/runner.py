"""nightly 실행기 보호(M6-2 리뷰 L2·r2 L4) — `all`이 예상 밖 예외(복구 중 Hyper-V 실패·gh 네트워크
실패 등)로 멈추면 시험 id `runner`(infra) 하나로 `runner-summary.json`을 남기고 publish를 시도한다.
그 run의 `summary.json`이 이미 있으면 덮어쓰지 않고 그것으로 publish를 다시 시도한다(이미 이력에
들어갔으면 `publish.step_publish`가 줄을 다시 쓰지 않는다). 종료 코드는 부른 쪽(`nightly.py`)이
정한다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import cast

from harness import publish, report
from harness.context import TAIL_CHARS, log, say, write_json
from harness.failures import INFRA
from harness.github import GitHub

RUNNER_TEST = "runner"
UNKNOWN_SHA = "0" * 40


def runner_summary(run_id: str, sha: str, today: date, exc: BaseException) -> dict[str, object]:
    """실행기 자신이 예상 밖 예외로 멈춘 실행 — 시험 id `runner` 한 줄(infra)로 남긴다."""
    failed = report.TestResult(RUNNER_TEST, report.FAILED, INFRA, 0.0)
    return report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=[failed],
        extra={"runner_error": repr(exc)[-TAIL_CHARS:]},
    )


def read_summary(path: Path, run_id: str) -> dict[str, object] | None:
    """이 run의 `summary.json`(없거나, 깨졌거나, 다른 run이면 None)."""
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    summary = cast(dict[str, object], data)
    if summary.get("run") != run_id or summary.get("verdict") not in report.STATUS_STATE:
        return None
    return summary


def report_failure(
    run_id: str, sha: str, today: date, out: Path, *, gh: GitHub | None, exc: BaseException
) -> dict[str, object]:
    """`exc`로 멈춘 run을 남긴다: `runner-summary.json` → (summary.json이 있으면 그것, 없으면
    runner summary로) publish 시도 → `publish.json`. 출력한 결과(`{"run", "verdict", "publish"}`)를
    돌려준다. publish가 실패해도 예외를 내지 않는다(`publish.json`에 남긴다)."""
    log(f"nightly runner failed: {exc!r}")
    runner = runner_summary(run_id, sha, today, exc)
    write_json(out / "runner-summary.json", runner)
    # 그 run의 실제 결과가 이미 있으면(예: publish·cleanup에서 멈춤) 덮어쓰지 않고 그것을 낸다
    # (리뷰 r2 L4) — 이미 이력에 들어갔으면 step_publish가 줄을 다시 쓰지 않는다.
    real = read_summary(out / "summary.json", run_id)
    summary = runner if real is None else real
    published: dict[str, object] = {"error": "not published (no GitHub client or sha)"}
    if gh is not None and sha != UNKNOWN_SHA:
        try:
            published = publish.step_publish(summary, gh, today)
        except Exception as publish_exc:
            published = {"error": f"publish failed: {publish_exc!r}"[-TAIL_CHARS:]}
    published["runner_error"] = runner["runner_error"]
    published["published_summary"] = "runner-summary.json" if real is None else "summary.json"
    write_json(out / "publish.json", published)
    shown: dict[str, object] = {"run": run_id, "verdict": summary["verdict"], "publish": published}
    say(shown)
    return shown
