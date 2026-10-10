"""M2d VM 실측의 release 쌍 — 하네스가 release를 아는 유일한 곳.

`run.py`(Windows 호스트)와 `bundles.py`(WSL runner)가 같이 읽는다. 설치 g1 = `BASE`,
업그레이드 `BASE`→`NEXT`(압축 chunk 이력이 있는 DB 위에서 실제 마이그레이션), 헬스 실패 롤백은
`FAULT`(`NEXT` 이미지에 collector만 세션을 맺지 않는 파생, `bundles.py fault-release`). crash는
`BASE`↔`NEXT` 사이를 오간다.

- M2 실측(run 20261005T055523Z, 30/30): r0(epoch 1)→r1(epoch 2).
- S3(2026-10-10 결정): r1(epoch 2)→r2(epoch 3, 0008·0009).

`BASE`·`NEXT`의 기록은 저장소 `deploy/releases/<이름>.json`, 이미지는 runner image store
`<store>/<이름>/<svc>.tar`(release_record.py)에 있어야 한다.
"""

from __future__ import annotations

BASE, NEXT, FAULT = "r1", "r2", "r2f"
RELEASES = {BASE: "2026.10.1", NEXT: "2026.10.2", FAULT: "2026.10.2-fault1"}
"""release 이름 → 달력 버전(fleet `product:`, release 기록 `version`)."""
EPOCHS = {BASE: 2, NEXT: 3, FAULT: 3}
"""기대 스키마 epoch. prepare가 release 기록의 `schema.epoch`와 대조한다(FAULT는 NEXT 스키마)."""
