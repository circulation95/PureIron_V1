"""G2 시나리오 단위 시험이 같이 쓰는 가짜 세션(M6-3a) — VM·WSL 없이 `g2.Session`·`SiteRun`을 만든다.

시험 파일이 아니다(`test_*`가 아니라 수집되지 않는다). `conftest.py`가 이 디렉터리를 `sys.path`에
넣는다.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from harness import g2, judges
from harness.context import Context
from harness.release_pair import nightly

SHA = "a" * 40
C0_GOOD: dict[str, object] = {"epoch": "5", "compressed_chunks": "2", "tag_history_rows": "6900"}
"""이력 시드 뒤 c0 기반 probe `db` — 압축 chunk·행 > 0, epoch = 쌍의 epoch 5."""


def session(tmp_path: Path) -> g2.Session:
    """nightly 쌍(BASE r5·NEXT n<sha7>, epoch 5) + 현장 둘. 카운터 기준은 c0 7·마지막 표시 9."""
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, 5)
    runs: dict[str, g2.SiteRun] = {}
    for base, _ in g2.SITE_ROLES:
        release = g2.site_release(pair, base.site_id)
        one = dataclasses.replace(base, state_file=tmp_path / base.site_id / "state.json")
        ctx = Context(
            run_id="20261110T180000Z",
            artifacts=str(tmp_path / base.site_id),
            current=release,
            site=one,
            pair=pair,
        )
        ctx.nft_total = dict.fromkeys(judges.AUDIT_COUNTERS, 0)
        ctx.nft_c0 = {"ext_out_v4": 7}
        ctx.nft_last = {"ext_out_v4": 9}
        runs[base.site_id] = g2.SiteRun(ctx, release, c0_at=0.0)
    return g2.Session("20261110T180000Z", SHA, pair, tmp_path, tmp_path / "sim.tar", runs)
