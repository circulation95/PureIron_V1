---
id: report-vm-e2e-runs
title: Hyper-V VM 실측 기록 — release 쌍별 M2d 하네스 결과
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - tests/e2e/README.md
  - tests/e2e/m2d/run.py
  - tests/e2e/m2d/release_pair.py
  - deploy/releases/r1.json
  - deploy/releases/r2.json
  - deploy/releases/r3.json
  - tests/e2e/infra/vm.py
last_verified: 2026-10-17
---

# Hyper-V VM 실측 기록

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 각 실측 run 시점의 **결과 기록**이다. 하네스·제품이 그 뒤에 바뀌었을 수 있으므로, 여기 적힌 수치를 근거로 결론을 쓰기 전에 해당 run의 `results.json`이나 새 실측으로 다시 확인한다. 새 실측을 하면 절을 더하고 `last_verified`를 갱신한다. 하네스 구성·판정의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md), 계약 쪽 요약은 [data-contracts](../sot/data-contracts.md)의 M2d VM 절이다.

M2d 하네스(`tests/e2e`, PD-24)를 release 쌍(`tests/e2e/m2d/release_pair.py`)마다 한 번씩 `run.py all`로 돌린 결과다. 항목은 매번 30개(`run.expected_items()`)이고, 결과 파일은 `artifacts/e2e/m2d-<run>/results.json`·`bundle-metrics.json`(gitignore)이다.

## M2 r0→r1 (run `20261005T055523Z`, 30/30 PASS)

plan M2 종료 조건 실측.

- 폐쇄망 부트스트랩: runbook 2단계(root 셸, 메타데이터만 추출 → `bootstrap-verify.sh` sha256·root 지문) → `install.sh --offline-time-confirmed` → enroll → g1(r0) 51 s, `/readyz` 200, overlay2 + classic image store, 실행 이미지 `.Id` == r0 config digest.
- 거부(VM 수동 1회): 다른 현장 `E_SITE`·변조 `E_DIGEST`·낮은 gen `E_GEN` exit 1, 같은 gen 재빌드는 R-7대로 no-op exit 0 — 모두 core `StartedAt`·실행 이미지 불변. 커밋 전 거부 보고는 gen을 믿지 않아 `g unknown`이라 번들 sha256 12자로 찾는다. 먼저 빌드한 낮은 gen 번들은 cert 단계 `max_seen_created_at` 역행(`E_CERT`)에 먼저 걸린다.
- 압축 chunk 3개가 있는 epoch 1 DB에서 r0→r1 24 s(`switching` 11 s, `migrating` 2 s) → epoch 2·`tag_history_1h`, 이력 행 보존.
- r1′(collector 세션 없음) → `E_HEALTH`, 134 s 안에 r0 digest 복귀 + `failed`(5분 이내, `checking` 90 s·`rolling_back` 19 s).
- crash 18회(phase `fetched`~`rolling_back` × `systemctl kill -s KILL` / `Restart-VM -Type Reset`): 모두 15~106 s 안에 수렴 — 결과·`applied_gen`·실행 이미지·편집 잠금 해제·경보 없음·이력 행 보존. 감시자가 phase 저장 직후 서비스 cgroup을 얼리므로 `migrating`은 "기록 뒤·다음 기록 전"을 끊는다(SQL 중간 중단은 core 통합 테스트 몫).
- nftables 외부 접속 시도(설치 전 기준값·설치 이후 누적, 복원·리셋 직전마다 누적) 0. 외부 DNS 패킷은 모두 systemd-resolved가 보내므로 하네스는 resolved 디버그 로그의 조회 이름을 출처 증거로 남긴다.
- 번들 크기·빌드 시간은 `bundle-metrics.json`(install 80 MB, 현장 번들 739 MB 중 site-db 599 MB, 빌드 약 58 s).
- 이 실측이 찾은 제품 결함 2건(서비스 롤백 `failed_manual`, TimescaleDB 텔레메트리)은 같은 PR에서 고쳤다 — 내용은 data-contracts M2d VM 절.

## S3 r1→r2 (run `20261009T231040Z`, 30/30 PASS, missing 0, 2026-10-10)

같은 하네스, 쌍만 `release_pair.py`로 바꿈(fault `r2f`). 제품 결함·하네스 수정 없이 한 번에 통과.

- 폐쇄망 부트스트랩 + g1(r1) 56 s. c0 = epoch 2(1분·1시간 CAGG) + 압축 chunk 3 + 이력 7141행.
- r1→r2 25 s(`switching` 12 s, `migrating` 2 s) → epoch 3, 1분·1시간 CAGG 실시간 집계, `project_state.historian` 열, 압축 chunk·이력 행 보존. 그 위에서 낮은 gen `E_GEN`. c0에서 `E_SITE`·`E_DIGEST` 거부와 같은 gen no-op도 M2와 같다(core `StartedAt`·실행 이미지 불변).
- r2′ → `E_HEALTH`, 135 s 안에 r1 digest 복귀 + `failed`(`checking` 90 s·`rolling_back` 20 s). crash 단계의 r2→r1 시도 7회도 `succeeded`(헬스 통과) — r1 앱(envelope [2, 3])이 epoch 3 DB에서 ready인 롤백 경로 실측.
- crash 18회 모두 16~109 s 안에 수렴(`fetched`·`verified` 92~109 s, 나머지 16~38 s, M2는 15~106 s). nftables 외부 접속 시도 누적 0.
- 번들: install 80 MB, r2 현장 번들 739 MB(site-db 599 MB, core 61 MB), 빌드 약 55 s.

## S4 r2→r3 (run `20261017T132857Z`, 30/30 PASS, missing 0, 2026-10-17)

같은 하네스, 쌍 r2→r3(fault `r3f`). 1차 run `20261017T113834Z`는 16/30 — 하네스 결함 1건을 고친 2차가 이 기록이다. 제품 코드는 바꾸지 않았다.

- 폐쇄망 부트스트랩 + g1(r2) 53 s. c0 = epoch 3(1분·1시간 CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, epoch 4 테이블 없음.
- r2→r3 25 s(`switching` 12 s, `migrating` 2 s) → epoch 4, `users`·`sessions`·`audit_log`·`alarms`·`alarm_journal`과 인덱스 5개, CAGG·BASE 열·압축 chunk·이력 행 보존. `E_SITE`·`E_DIGEST`·`E_GEN` 거부와 같은 gen no-op은 M2·S3와 같다(core `StartedAt`·실행 이미지 불변).
- r3′ → `E_HEALTH`, 135 s 안에 r2 digest 복귀 + `failed`(`checking` 90 s·`rolling_back` 20 s). crash 단계의 r3→r2 시도도 `succeeded` — r2 앱(envelope [3, 4])이 epoch 4 DB에서 ready인 롤백 경로 실측.
- crash 18회 모두 16~99 s 안에 수렴(`fetched`·`verified` 94~99 s, 나머지 16~57 s). service 대상 16회는 스테이징이 창보다 78~82 s 먼저 끝났다(apply rc 0, 1회). nftables 외부 접속 시도 누적 0.
- 번들: install 80 MB, r3 현장 번들 740 MB(site-db 599 MB, core 61 MB), 빌드 약 60 s.
- **1차 실패와 수정**: crash 14건이 `staged_before_window`만 false(복구 판정은 모두 true). 14건은 정확히 "apply ssh가 끝날 때 crash 감시자 ssh가 살아 있던" 케이스였다(감시자가 먼저 끝나는 `verified`, CLI 대상 `fetched`는 통과). 실측을 콘솔에서 띄우면 동시에 도는 ssh 둘이 같은 콘솔 stdin을 물려받는다 → `tests/e2e/infra/vm.py`의 ssh·scp를 모두 `stdin=DEVNULL`(`ssh -n`과 같음)로 띄우게 고쳤다. 2차는 같은 조건(콘솔 실행, 14건 모두 `watcher_alive` true)에서 통과했다. 하네스는 crash마다 `stage`(apply rc·시도 횟수·창까지 여유·감시자 생존)를 결과에 남긴다.
