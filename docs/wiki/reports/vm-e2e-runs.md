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
  - deploy/releases/r4.json
  - tests/e2e/infra/vm.py
last_verified: 2026-10-17
---

# Hyper-V VM 실측 기록

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 각 실측 run 시점의 **결과 기록**이다. 하네스·제품이 그 뒤에 바뀌었을 수 있으므로, 여기 적힌 수치를 근거로 결론을 쓰기 전에 해당 run의 `results.json`이나 새 실측으로 다시 확인한다. 새 실측을 하면 절을 더하고 `last_verified`를 갱신한다. 하네스 구성·판정의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md), 계약 쪽 요약은 [data-contracts](../sot/data-contracts.md)의 M2d VM 절이다.

M2d 하네스(`tests/e2e`, PD-24)를 release 쌍(`tests/e2e/m2d/release_pair.py`)마다 한 번씩 `run.py all`로 돌린 결과다. 항목은 S4까지 30개, M5 마감부터 35개(`run.expected_items()`)이고, 결과 파일은 `artifacts/e2e/m2d-<run>/results.json`·`bundle-metrics.json`(gitignore)이다.

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

## M5 마감 r3→r4 (run `<VM 실측 뒤 기록>`, `<결과>`/35, 초안 2026-11-01)

> **초안** — 하네스·로컬 사전 검증까지의 기록이다. run id·결과·수치는 VM 실측 뒤 채운다(`<…>` 자리). 그 전에는 이 절을 실측 근거로 쓰지 않는다.

같은 하네스, 쌍 r3→r4(fault `r4f`). r4는 Updater가 번들 프로젝트 트리를 적용하는 첫 release다(M5-0). 그래서 r3→r4 업그레이드가 VM에서 처음 실제 트리를 적용하고, 항목이 35개로 늘었다(S4의 30개 + M5 확인 4개 + 전환기 경보 해제 1개).

**항목 대응(S4 30개 → 35개).** 이름이 쌍을 따라 바뀐 것과 새 항목만 적는다. 나머지(`vm boot`·`OS quiet`·`reject site`·`reject tampered`·`same gen rebuild → no-op`·`reject lower gen`·crash 18개·`no external connection …`)는 이름 그대로다.

| S4(30) | M5 마감(35) | 판정 변화 |
|---|---|---|
| `install bundle bootstrap + g1(r2) on offline VM` | `install bundle bootstrap + g1(r3) on offline VM` | c0 = epoch 4: BASE 산출물에 0010~0012 테이블·인덱스 포함, `project_files` 없음 |
| `r2→r3 on compressed epoch-3 DB` | `r3→r4 on compressed epoch-4 DB` | 같음(첫 트리 적용 포함) |
| `r3 schema after upgrade` | `r4 schema after upgrade` | `next_tables` = `project_files`, `next_indexes` 없음(0013에 이름 붙은 인덱스 없음), `base_tables`·`base_indexes`(epoch 4 산출물 유지) 추가 |
| — | `r4 project tree applied (project_files, definitions, base_site_rev)` | 새 ⓐ |
| — | `site PATCH via web (admin login, tag + alarm) → site_rev +2` | 새 ⓑ |
| — | `reject unabsorbed site drafts (E_DRAFT)` | 새 ⓒ |
| — | `draft export → absorb → gate → reapply (PATCH kept, base_site_rev == site_rev)` | 새 ⓓ |
| `r3' health failure → rollback` | `r4' health failure → rollback (transition, E_DEFINITIONS_LEFT)` | 경보 없음 → **전환기 경보 기대**(아래) |
| `rollback within 5 min to r2 digests` | `rollback within 5 min to r3 digests` | 같음(300 s) |
| — | `r4 tree bundle after transition rollback → succeeded, alarm cleared` | 새(전환기 경보 해제) |

**전환기 경보 판정.** `c0`(r3, 트리 이전)에서 `r4f`를 적용하면 r4 이미지가 트리를 적용한 뒤 헬스에서 실패하고, 백업을 만든 직전 r3 core는 트리를 모르므로 롤백은 project_state만 되돌린다 — r4f가 적용한 장비·태그·알람 정의는 남는다. 이것은 설계된 동작이고(updater README runbook `E_DEFINITIONS_LEFT`, data-contracts M5-0 한계) 제품 결함이 아니다. 하네스는 다음을 모두 요구한다: 결과 `failed`, 실행 이미지 = r3 digest(5분 안), Updater 경보에 `… E_DEFINITIONS_LEFT` 항목, 보고 `logs`에 `E_DEFINITIONS_LEFT`·`failed_manual` false(자동 롤백 자체는 성공), 메트릭 `updater_failed_manual 1`. 이어서 그 상태에서 정상 r4 트리 번들을 적용하면 `succeeded`와 경보·메트릭 해제(0)를 요구한다 — 경보는 정의를 실제로 교체한 트리 release 성공만 지운다(리뷰 N-1). runbook 수동 복구(`apply-definitions`를 직전 이미지로)는 VM에서 하지 않는다(단위·recover 하네스가 고정). crash의 `rolling_back` 2건은 r4 위의 r4f라 트리 백업이 있어 경보가 없어야 한다(기존 판정).

**M5 확인 4개**(업그레이드 r4 위, 낮은 gen 거부 다음, FAULT용 c0 복원 앞):

- ⓐ 트리 적용: `GET /api/v1/project` version `v1`·content_hash = fixture `acme`, `site_rev == base_site_rev`; DB `project_files` 4행, retire되지 않은 장비·태그·알람 = fixture(c0 시드 장비·태그는 트리 교체로 retire).
- ⓑ 현장 PATCH: core 컨테이너 `pureiron-core users add e2e-admin --role admin`(비밀번호는 VM에서 생성) → web 경유 로그인(세션 쿠키) → `line1/temp` record·`temp-high` setpoint PATCH(비안전 요청마다 `Origin`·`Sec-Fetch-Site: same-origin`) → site_rev 0→2, base 0 그대로, DB 정의 = PATCH 값.
- ⓒ 드래프트 게이트: absorb하지 않은 repo로 만든 g3 → exit 1·`E_DRAFT`·core `StartedAt`·이미지 불변. fixture는 `absorbs: {site-a: 3}`이라 그대로 쓰면 site_rev 2 ≤ 3으로 게이트가 통과한다 — 하네스는 `absorbs`만 뺀 사본을 쓴다.
- ⓓ export→absorb→재적용: `pureiron-site project export` + `state/enrollment.json` → 호스트 → WSL `pureiron-project absorb --draft --site-key` → `gate --draft` → 그 트리로 g4 → `succeeded`, content_hash = 흡수한 트리, `base_site_rev == site_rev == 2`, PATCH 값 유지, 편집 잠금 해제.

**로컬 사전 검증(VM 전, 2026-11-01).** compose `pureiron-it-m5c`에서 Updater 순서(r3 migrate → r3 메타 apply → 시드 → r4 migrate → r4 `project apply --tar`)를 손으로 밟아 하네스 판정 함수에 실측값을 넣었다: c0·NEXT 스키마·ⓐ·ⓑ(교차 Origin PATCH는 403 `E_ORIGIN`)·ⓒ(`verify_bundle`이 드래프트 있으면 `E_DRAFT`, 없으면 통과, CLI `gate` exit 1)·ⓓ(실제 `export_draft` 서명 → absorb `absorbs[site-a]: (absent) -> 2` → gate 통과 → 재적용 base = site_rev = 2) 모두 통과. 트리를 적용한 epoch 5 DB 위 r3 core·collector·web(r4→r3 하강)은 readyz 200, `migrate status` cur 5·app_expected 4·`unknown_applied` [13], 태그 5/5 GOOD, core·collector 오류 로그 0, 다시 r4로 올려도 같다. r4 위 r3 메타 apply(트리 유지)·r4 트리 백업(`export --with-state`)·복원도 통과. Updater 전체 경로(잠금·백업·롤백·경보·메트릭·보고)와 uid 10001 권한 경로는 VM 몫이다.

- 폐쇄망 부트스트랩 + g1(r3) `<s>`. c0 = epoch 4 + 압축 chunk `<n>`, `project_files` 없음.
- r3→r4 `<s>`(`switching` `<s>`, `migrating` `<s>`) → epoch 5, `project_files` `<n>`행.
- M5 ⓐ~ⓓ: `<결과>`.
- r4′ → `E_HEALTH` + `E_DEFINITIONS_LEFT`, `<s>` 안에 r3 digest 복귀 + `failed`, 경보 해제 `<결과>`.
- crash 18회 `<범위>` 안에 수렴, r4↔r3 오가는 시도(트리 적용된 epoch 5 DB 위 r3 앱 하강 포함) `<결과>`. nftables 외부 접속 시도 누적 `<n>`.
- 번들: install `<MB>`, r4 현장 번들 `<MB>`, 빌드 `<s>`.
