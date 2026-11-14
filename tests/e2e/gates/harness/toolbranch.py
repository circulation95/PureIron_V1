"""fleet 도구 브랜치(M6-4a A3, 계획 ★P2) — V1 `e2e/nightly-tool`을 run마다 HEAD + nightly release
기록 커밋으로 force 갱신한다.

publish는 product 기록을 **도구 checkout의 `deploy/releases`**에서 찾는다(`publish.py` F4) — HEAD
빌드의 NEXT·FAULT 기록은 WSL nightly 작업 디렉터리에만 있으므로, fleet-test workflow가 보는 고정
브랜치에 그 기록을 커밋한다. 형식은 저장소 `deploy/releases/*.json`과 같다(indent 2·키 정렬·끝 개행
— release_record.py·bundles.py). 커밋은 GitHub git data API로(blob → `base_tree` = HEAD 트리 →
commit(부모 HEAD) → ref force)이고 계정은 circulation95 keyring이다: Actions `GITHUB_TOKEN`은
`workflows` 권한을 가질 수 없어 workflow 파일이 바뀐 커밋으로 ref를 옮기는 갱신을 거부한다(HEAD가
어제와 다른 `.github/workflows`를 가지면 매번 걸린다). HEAD가 GitHub에 없으면(로컬 커밋)
`FleetError` (infra).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from harness import fleet
from harness.context import E2EError
from harness.fleet import Repo

TOOL_BRANCH = "e2e/nightly-tool"


def record_text(record: Mapping[str, object]) -> bytes:
    """`deploy/releases/*.json`과 같은 형식(release_record.py·bundles.py: indent 2·키 정렬·끝
    개행)."""
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def update_tool_branch(
    v1: Repo, head_sha: str, records: Mapping[str, Mapping[str, object]], run_id: str
) -> dict[str, object]:
    """`e2e/nightly-tool` = HEAD + `deploy/releases/<이름>.json`(NEXT·FAULT 2종) 커밋(force). HEAD가
    GitHub에 없으면(로컬 커밋) `FleetError`."""
    if not records:
        raise E2EError("no nightly release records to commit to the tool branch")
    files = {f"deploy/releases/{name}.json": record_text(rec) for name, rec in records.items()}
    message = fleet.bot_title(run_id, f"도구 브랜치: {', '.join(sorted(records))} 기록")
    commit = v1.commit_files(head_sha, files, message)
    v1.force_branch(TOOL_BRANCH, commit)
    return {"branch": TOOL_BRANCH, "commit": commit, "head": head_sha, "records": sorted(records)}
