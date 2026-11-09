---
id: report-vm-e2e-runs
title: Hyper-V VM 실측 기록 — release 쌍별 M2d 하네스 결과
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - tests/e2e/README.md
  - tests/e2e/m2d/run.py
  - tests/e2e/gates/harness/release_pair.py
  - deploy/releases/r1.json
  - deploy/releases/r2.json
  - deploy/releases/r3.json
  - deploy/releases/r4.json
  - deploy/releases/r5.json
  - tests/e2e/gates/harness/central.py
  - tests/e2e/gates/harness/vm.py
  - tests/e2e/gates/harness/golden.py
last_verified: 2026-11-09
---

# Hyper-V VM 실측 기록

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 각 실측 run 시점의 **결과 기록**이다. 하네스·제품이 그 뒤에 바뀌었을 수 있으므로, 여기 적힌 수치를 근거로 결론을 쓰기 전에 해당 run의 `results.json`이나 새 실측으로 다시 확인한다. 새 실측을 하면 절을 더하고 `last_verified`를 갱신한다. 하네스 구성·판정의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md), 계약 쪽 요약은 [data-contracts](../sot/data-contracts.md)의 M2d VM 절이다.

M2d 하네스(`tests/e2e`, PD-24)를 release 쌍(`tests/e2e/gates/harness/release_pair.py`)마다 한 번씩 `run.py all`로 돌린 결과다. 항목은 S4까지 30개, M5 마감 35개, M4 마감부터 39개(`run.expected_items()`)이고, 결과 파일은 `artifacts/e2e/m2d-<run>/results.json`·`bundle-metrics.json`(gitignore)이다.

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
- **1차 실패와 수정**: crash 14건이 `staged_before_window`만 false(복구 판정은 모두 true). 14건은 정확히 "apply ssh가 끝날 때 crash 감시자 ssh가 살아 있던" 케이스였다(감시자가 먼저 끝나는 `verified`, CLI 대상 `fetched`는 통과). 실측을 콘솔에서 띄우면 동시에 도는 ssh 둘이 같은 콘솔 stdin을 물려받는다 → `tests/e2e/gates/harness/vm.py`의 ssh·scp를 모두 `stdin=DEVNULL`(`ssh -n`과 같음)로 띄우게 고쳤다. 2차는 같은 조건(콘솔 실행, 14건 모두 `watcher_alive` true)에서 통과했다. 하네스는 crash마다 `stage`(apply rc·시도 횟수·창까지 여유·감시자 생존)를 결과에 남긴다.

## M5 마감 r3→r4 (run `20261101T035728Z`, 35/35 PASS, missing 0, 2026-11-01)

하네스 HEAD `072df90`(`chore/m5-close-r4`), 제품 r4 = `d115f95` 기록(2026.11.0, epoch 5). 1차 run `20261101T022914Z`(HEAD `6d0a6dc`)는 34/35 — 하네스 판정 결함 1건을 고친 2차가 이 기록이다(맨 아래 "1차 실패와 수정"). 제품 코드는 바꾸지 않았다.

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

**실측 결과(2차, `results.json` 그대로).**

- 폐쇄망 부트스트랩 + g1(r3) 52.7 s, `/readyz` 200, overlay2 + classic image store, 실행 이미지 = r3. c0 = epoch 4 + 압축 chunk 3 + 이력 7141행, `project_files` 없음(c0 판정 `epoch`·`base_schema`·`next_absent`·`compressed_chunks`·`seed_rows` 모두 참).
- 거부: `E_SITE`·`E_DIGEST` exit 1, 같은 gen 재빌드 no-op exit 0, r4 위 낮은 gen `E_GEN` exit 1 — 모두 core `StartedAt`·실행 이미지 불변(S4와 같음).
- r3→r4(g2) 25.4 s(`switching` 11.5 s, `fetched` 4.5 s, `migrating` 2.1 s, `locked` 1.9 s, 나머지 각 0.1 s 미만) → `succeeded`, epoch 5, `project_files` 4행, 장비 `plc-1`·태그 5·알람 `temp-high`·`temp-low`(c0 시드 `e2e-dev`·`e2e/tag`는 retire). CAGG 2개 실시간 집계·BASE 열/테이블/인덱스·압축 chunk 3·이력 7141행 보존, 경보 없음, `updater_failed_manual` 0.
- M5 ⓐ~ⓓ 4/4 통과.
  - ⓐ `GET /api/v1/project` = `acme` `v1`, content_hash `sha256:bf7f88d2…99c6`, `site_rev` 0 == `base_site_rev` 0, DB `project_files` 4행·정의 = fixture.
  - ⓑ `e2e-admin` 생성 rc 0 → web 로그인 200(세션 쿠키) → `line1/temp` record PATCH 200(`site_rev` 1, deadband 0.5→0.8·max_interval_s 60→30) → `temp-high` setpoint PATCH 200(80→82.5, `site_rev` 2) → readback = PATCH 값, base 0 그대로.
  - ⓒ absorb 안 한 repo로 만든 g3 → exit 1 `E_DRAFT`, core `StartedAt`·이미지 불변.
  - ⓓ export(`site_rev` 2, base 0, draft hash `sha256:361cb5d2…1fed`) → absorb rc 0(`absorbs[site-a]: (absent) -> 2`, 바뀐 파일 3개) → `gate --draft` rc 0 → g4 `succeeded` 13.8 s, content_hash = 흡수 트리, `base_site_rev == site_rev == 2`, PATCH 값 유지, 편집 잠금 해제.
- 전환기 롤백(c0 위 r4f g2): `failed`, code `E_HEALTH`, 135.0 s 안에 r3 digest 복귀(5분 기준 통과; `checking` 90.2 s·`rolling_back` 19.3 s·`switching` 11.4 s). Updater 경보 `g2: E_HEALTH E_DEFINITIONS_LEFT`, `expected_alarm`(경보 항목·보고 `logs`의 `E_DEFINITIONS_LEFT`·`failed_manual` false) 참, 메트릭 `updater_failed_manual` 1. 편집 잠금 해제·이력 행 보존.
- 경보 해제: 그 상태에서 정상 r4 트리 번들 g5 → `succeeded` 22.5 s, 경보 없음, `updater_failed_manual` 0. 트리 재적용 판정(`project_version`·`base_site_rev`·`project_files`·`devices`·`tags`·`alarms`) 모두 참 — 경보는 정의를 실제로 교체한 트리 release 성공이 지웠다.
- crash 18회 모두 16.7~105.5 s 안에 수렴(`fetched`·`verified` 94.6~105.5 s, 나머지 16.7~53.4 s). r4↔r3를 오가며 r3 시도 7회(g7~g19 홀수, 트리를 적용한 epoch 5 DB 위 r3 앱 하강)도 모두 `succeeded`. `rolling_back` 2건(r4 위 r4f)은 `failed`/`E_HEALTH`이고 트리 백업이 있어 경보 없음. service 대상 16회는 스테이징이 창보다 75.5~80.4 s 먼저 끝났다(apply rc 0, 1회).
- 외부 접속: `vm` 기준값 5개 카운터 모두 0. 설치 이후 누적 `ext_out_v4` 0·`dns_out` 0·`ext_out_v6` 0·`ext_fwd` 0, `field_fwd` 217. `dns_lookups` `{core: 12}`는 판정 대상이 아니다 — 하네스 README 설명대로 core 컨테이너가 없는 동안(crash 구간) Docker 내장 DNS가 resolved로 넘긴 이름이고, `dns_out` 0이라 resolved가 로컬에서 답했다.
- **"외부 접속 시도 0"의 정의가 S4까지와 다르다.** M5 마감부터 선언 장비 폴링 `field_fwd`는 판정에서 뺀다. `field_fwd`는 eth0으로 나가는 forward 중 IPv4·TCP·번들 트리가 선언한 장비 host·port 쌍(이번 fixture `10.0.0.5:502`)만이다. 다른 목적지·포트, UDP, IPv6, VM 자신의 송신(`audit_out`)은 여전히 `ext_*`·`dns_out`에 잡힌다. S4까지는 트리가 적용되지 않아 collector가 장비를 폴링하지 않았으므로 이 구분이 없었다. 정본 정의는 [`tests/e2e/README.md`](../../../tests/e2e/README.md) nftables 절.
- 번들: install 80 MB, r4 현장 번들 745 MB(site-db 599 MB, core 66 MB), r3 740 MB, 현장 번들 24개 빌드 각 59~63 s.
- **1차 실패와 수정**: 1차 run `20261101T022914Z`(HEAD `6d0a6dc`)는 34/35로 `no external connection …`만 실패했다 — 누적 `ext_fwd` 216, 나머지 0. 커널 로그 216건이 모두 `172.18.0.5 → 10.0.0.5:502` TCP SYN(collector → fixture 장비 `plc-1`)이었다. r4가 처음 트리를 적용해 collector가 선언 장비를 폴링하기 시작한 것이라 외부 접속이 아니고, 하네스가 이를 외부로 센 판정 결함이었다. `072df90`에서 선언 장비 끝점을 `field_fwd` 규칙으로 `ext_fwd`보다 앞에 분리하고 판정에서 그 키만 뺐다(단위 테스트가 규칙 위치·끝점 추출·판정을 고정). 2차는 같은 폴링이 `field_fwd` 217로 가고 `ext_fwd` 0이었다.

## M4 마감 r4→r5 (run `20261108T205941Z`, 39/39 PASS, missing 0, 2026-11-09)

하네스 HEAD `b75755e`(`chore/m4-close-r5`)로 `run.py all --keep-central`을 돌렸고, 온라인 ①~④와 `report`만 `cf296cb`에서 다시 돌렸다(`run.py online --keep-central` + `report`, EXIT 0). 첫 전체 실행은 38/39였다 — 하네스 대기 조건 결함 1건을 고친 재실행까지 합친 결과가 이 기록이다(맨 아래 "1차 실패와 수정"). VM e2e 때문에 바꾼 제품 코드는 없다. 15분 실측이 찾은 publish 결함은 아래 15분 실측 절에 적었다.

쌍 r4→r5(fault `r5f`). r5 = `329ec2c` 기록(2026.11.1, epoch 5) — **현장 마이그레이션이 없다**(같은 epoch). 그래서 업그레이드·crash `migrating`은 phase만 지나가고 SQL은 돌지 않는다. r4가 이미 트리 적용 release라 c0(r4)의 FAULT는 전환기가 아니라 트리 백업이 있는 일반 롤백(경보 없음)이다. 새 `online` 단계가 c0 위에서 WSL 중앙 c0(`deploy/central` compose, `10.77.0.1:28443`, Caddy 사설 CA)에 VM을 붙인다.

**항목 대응(M5 마감 35개 → 39개).** 이름이 쌍을 따라 바뀐 것과 새·바뀐 판정만 적는다. 나머지(`vm boot`·`OS quiet`·`reject site`·`reject tampered`·`same gen rebuild → no-op`·`reject lower gen`·M5 확인 ⓑ~ⓓ·crash 18개·`no external connection …`)는 이름 그대로다.

| M5 마감(35) | M4 마감(39) | 판정 변화 |
|---|---|---|
| `install bundle bootstrap + g1(r3) on offline VM` | `install bundle bootstrap + g1(r4) on offline VM` | c0 = epoch 5(`project_files` 포함, BASE_TABLES로 옮김), `next_absent` 제거(NEXT 산출물 없음) |
| `r3→r4 on compressed epoch-4 DB` | `r4→r5 on compressed epoch-5 DB` | 마이그레이션 없음(phase만) |
| `r4 schema after upgrade` | `r5 schema after upgrade` | `next_tables` 제거, BASE 산출물(0010~0013) 유지만 |
| `r4 project tree applied …` | `r5 project tree applied …` | 업그레이드 번들이 NEXT 트리(fixture에서 `line1/temp_sp` description만 `… (r5)`)를 싣는다 — 같은 트리면 core no-op이라 c0 시드가 남아 실패(수정 r1, 리뷰 H1). 판정에 c0 시드 retire·그 태그의 DB 정의 값 추가 |
| `r4' health failure → rollback (transition, E_DEFINITIONS_LEFT)` | `r5' health failure → rollback (no alarm, r4 tree and definitions back)` | 전환기 경보 기대 → **경보 없음**, FAULT가 FAULT 트리(`line1/temp_sp` description `… (r5f)`)를 실어 롤백 뒤 content_hash·DB 정의 값 = fixture(c0) |
| `rollback within 5 min to r3 digests` | `rollback within 5 min to r4 digests` | 같음(300 s) |
| `r4 tree bundle after transition rollback → succeeded, alarm cleared` | `r5 retry after r5' rollback → succeeded` | 경보 해제 → 롤백 뒤 NEXT 재시도 성공(경보·메트릭 0, NEXT 트리·정의 값 = NEXT) |
| — | `online: HTTPS heartbeat to central c0 (private CA trusted) → /fleet online` | 새 ① |
| — | `online: central-published r4 g2 → download + apply succeeded` | 새 ② |
| — | `online: ApplyAttempt report → /fleet current + last_attempt (source online)` | 새 ③ |
| — | `online: central web down → site keeps running, /fleet stale → back online` | 새 ④ |

**사전 검증(VM 전, 워커, 2026-11-09).** 중앙 헬퍼 `central.py`를 WSL에서 `10.77.0.1` bind로 띄워 `curl --cacert` healthz 200, CA 없이 `curl` 실패(rc 60), 컨테이너(`python:3.12-slim`)에서 CA를 OS 신뢰 저장소에 넣기 전 `CERTIFICATE_VERIFY_FAILED` → 넣은 뒤 `http.client`·`urllib` 200(D8 규칙이 publish·Updater의 기본 SSL 설정으로 동작). 실제 r4 g2 번들(744,806,400 B)을 ci 토큰으로 HTTPS PUT 201(5.4 s), VM Updater와 같은 형식의 서명 heartbeat·보고 → `/fleet` online·current g2·last_attempt `succeeded`(`source` online), control-web 중지 1 s 뒤 online·36 s 뒤 `stale`(control-api 직접 조회), 재기동 뒤 heartbeat로 online. 로컬 site-db에서 r4 설치 → 시드 → r5 업그레이드 순서의 probe `db`는 c0와 r5가 바이트까지 같았다(마이그레이션 없음 확인). **수정 r1(리뷰 H1)**: 그 같음은 같은 트리 재적용이 no-op(`changed=False`)이라 c0 시드 장비·태그가 retire되지 않았다는 뜻이기도 해, 그대로면 ⓐ가 VM에서 실패한다. 로컬 site-db + core r4·r5 CLI로 다시 재서(`tests/e2e/tests/fixtures/m4c-tree-db.json`) NEXT 트리 업그레이드는 `changed=True`·시드 retire·`line1/temp_sp` 정의 = `… (r5)`, FAULT 트리 적용 → r4 백업 복원은 정의 = fixture, 이어서 NEXT 재시도는 정의 = `… (r5)`임을 확인하고 판정 시험에 고정했다. VM Updater의 실제 HTTPS polling·적용·보고는 VM 몫이다.

**실측 결과(`results.json` 그대로, 온라인 ①~④는 재실행 값).**

- 폐쇄망 부트스트랩 + g1(r4) 53.8 s, `/readyz` 200, overlay2 + classic image store, 실행 이미지 = r4. c0 = epoch 5 + 압축 chunk 3 + 이력 7141행 + `project_files` 4행, 장비 `e2e-dev`·`plc-1`(c0 판정 `epoch`·`base_schema`·`compressed_chunks`·`seed_rows` 모두 참).
- 거부: `E_SITE`·`E_DIGEST` exit 1, 같은 gen 재빌드 no-op exit 0, r5 위 낮은 gen `E_GEN` exit 1 — 모두 core `StartedAt`·실행 이미지 불변.
- r4→r5(g2) 24.0 s(`switching` 11.4 s, `fetched` 4.7 s, `locked` 1.9 s, `migrating` 0.6 s, 나머지 각 0.1 s 미만) → `succeeded`, epoch 5 그대로. CAGG 2개 실시간 집계·BASE 열/테이블/인덱스·압축 chunk 3·이력 행 보존, 경보 없음, `updater_failed_manual` 0.
- ⓐ 트리 적용: `GET /api/v1/project` = `acme` `v1`, content_hash `sha256:b4fa1c9a…3ed8`, `site_rev` 0 == `base_site_rev` 0. DB `project_files` 4행, 장비 `plc-1`·태그 5개(c0 시드 `e2e-dev`·`e2e/tag` retire), `line1/temp_sp` description `1라인 온도 설정값 (r5)`(판정 `seed_retired`·`definition` 참).
- ⓑ~ⓓ 통과.
  - ⓑ `e2e-admin` 생성 rc 0 → web 로그인 200 → `line1/temp` record PATCH 200(`site_rev` 1, deadband 0.5→0.8·max_interval_s 60→30) → `temp-high` setpoint PATCH 200(80→82.5, `site_rev` 2) → readback = PATCH 값, base 0 그대로.
  - ⓒ absorb 안 한 repo로 만든 번들 → exit 1 `E_DRAFT`, core `StartedAt`·이미지 불변.
  - ⓓ export(`site_rev` 2, base 0, draft hash `sha256:53194037…9d9b`) → absorb rc 0(`absorbs[site-a]: (absent) -> 2`, 바뀐 파일 3개) → `gate --draft` rc 0 → g4 `succeeded` 13.8 s, `base_site_rev == site_rev == 2`, PATCH 값 유지, 편집 잠금 해제.
- 롤백(c0 위 r5f g2): `failed`, code `E_HEALTH`, 134.4 s 안에 r4 digest 복귀(5분 기준 통과; `checking` 90.2 s·`rolling_back` 19.3 s·`switching` 11.5 s). 경보 없음, `updater_failed_manual` 0, r4 트리·정의 복귀 판정(`project_version`·`base_site_rev`·`project_files`·`devices`·`tags`·`alarms`·`seed_retired`·`definition`) 모두 참. 편집 잠금 해제·이력 행 보존.
- 재시도: 그 상태에서 r5 g5 → `succeeded` 23.8 s(`switching` 12.1 s), 경보·메트릭 0, NEXT 트리·정의 판정 모두 참.
- crash 18회 모두 16.8~102.8 s 안에 수렴(`fetched`·`verified` 98.8~102.8 s, 나머지 16.8~53.3 s). r4↔r5를 오가며 r4 시도 7회(g7~g19 홀수)도 모두 `succeeded`. `rolling_back` 2건(r5f g21·g22)은 `failed`/`E_HEALTH`이고 경보 없음. service 대상 16회는 스테이징이 창보다 74.7~79.1 s 먼저 끝났다(apply rc 0, 1회).
- 온라인 ①~④(c0 위, 중앙 c0 `https://10.77.0.1:28443/api/v1`, Updater `PUREIRON_POLL_INTERVAL_S=10`).
  - ① VM이 신뢰한 CA 지문 = 중앙 CA 지문(`97:D5:B7:…:28:D7`), site-a 등록(현장 키 `629d5a0d…f0cf`). 등록·Updater 재시작 뒤 11.3 s에 행 판정 항목이 모두 찼다: `connection` online, heartbeat seq 3, current g1 `2026.11.0`/`v1`(source online). 등록 자체에 걸린 시간은 기록 없음.
  - ② r4 g2(744,806,400 B) PUT 201(게시 22:59:26Z) → desired g2 → Updater가 polling으로 받아 `succeeded`. PUT 뒤 수렴 34.7 s, 시도 22:59:43→22:59:55Z(`switching` 4.4 s·`locked` 3.4 s·`fetched` 3.3 s). 실행 이미지 = r4, 경보·메트릭 0. 다운로드만의 시간과 PUT 업로드 시간은 기록 없음.
  - ③ 수렴 뒤 5.9 s에 `/fleet` current g2(source online)·last_attempt 1→2 `succeeded`(report_seq 15).
  - ④ control-web(Caddy) 중지 → 48.1 s 뒤 조회에서 `stale`. 하네스는 고정 대기 뒤 한 번 보므로 stale이 된 시점 자체는 기록 없음. 그동안 현장 ready, 실행 이미지 = r4, core·Updater 재시작 없음, 경보·새 시도 없음. 재기동 뒤 5.8 s에 online(heartbeat seq 21).
- 외부 접속: `vm` 기준값 5개 카운터 모두 0. 설치 이후 누적 `ext_out_v4` 0·`dns_out` 0·`ext_out_v6` 0·`ext_fwd` 0, `field_fwd` 351(선언 장비 폴링, M5 마감부터 판정 제외). `dns_lookups` `{core: 12}`는 M5 마감과 같은 이유로 판정 대상이 아니다.
- 번들: install 80 MB, r4·r5 현장 번들 각 745 MB(744,806,400 B; site-db 599 MB, core 66 MB), 현장 번들 25개 빌드 59.8~75.9 s(가장 긴 것은 온라인 단계 r4 g2).

**15분 실측(★D6, 2026-11-09, 시각은 UTC).** fleet repo `circulation95/pureiron-fleet-test`, 중앙 c0(`https://10.77.0.1:28443/api/v1`, Caddy TLS·사설 CA), 현장 VM site-a, self-hosted runner. 승인과 merge는 **에이전트 토큰**(작성 ws-blip, 승인·merge circulation95)으로 했고, 승인 기록(`pulls/N/reviews`)을 확인한 뒤 merge했다. 그래서 아래 값은 **기계 시간**이다. 사람이 승인하는 시간과 새 README 절차를 사람이 따라 하는 것은 이 측정으로 검증되지 않았다.

성공한 실측은 PR #19(site-a gen 5, r4→r5, VM g4→g5)다.

| 단계 | 시각 | t0부터 |
|---|---|---|
| t0(브랜치 생성 시작) | 03:19:35 | 0:00 |
| PR 열림 | 03:19:39 | 0:04 |
| check-gen success | 03:20:11 | 0:36 |
| 승인(reviews 확인) | 03:20:12 | 0:37 |
| merge `624c80d` | 03:20:15 | 0:40 |
| 중앙 게시(published_at) | 03:20:58 | 1:23 |
| VM 적용 시작(attempt g4→g5) | 03:21:02 | 1:27 |
| publish run 42555841403 success | 03:21:10 | 1:35 |
| VM 적용 succeeded | 03:21:26 | 1:51 |
| 중앙 `/fleet` current gen 5·2026.11.1 반영 | 03:21:36 | 2:01 |

- t0→VM 성공 **1분 51초**, 중앙 반영까지 **2분 1초** — 15분 기준 통과(기계 시간).
- VM: `applied_gen` 5, `last_result` succeeded, `schema_epoch` 5. core·collector·web이 r5로 재시작했고 이미지 ID = `deploy/releases/r5.json` `config_digest`(core `fd6264b3…`, collector `d78476bd…`, web `852c4f07…`), site-db는 그대로(r5 site-db == r4). 번들 sha256 `f6074c9f…044b`, 744,806,400 B.
- control-web: basic auth(`ops`)로 `/` 200(현장 현황 SPA), `/ui/fleet`에서 site-a desired·current gen 5·2026.11.1·online·last_attempt 4→5 succeeded. 브라우저 화면 렌더링은 육안으로 확인하지 않았다.
- 측정 전에 no-op publish(workflow_dispatch, "건너뜀 1", 3초)로 runner 배정을 확인했다.

실패·중단 시도와 전환 PR:

- #15(merged `1f9e46b`, 측정 아님): workflow를 V1 도구 `@cf296cb`와 `control_api` 중앙 c0로 전환.
- #16(site-a gen 3, merged `92b32bf`) **실패**: publish run 42536145640 `E_PUBLISH … PUT /bundles/site-a/3 failed: The read operation timed out`. `put_bundle`의 이른 응답 감지가 TLS 1.3 세션 티켓을 HTTP 응답으로 봤다(리허설 M4-2는 평문 HTTP라 드러나지 않음). 수정 r3 `af99c61` — 동작 설명은 [data-contracts](../sot/data-contracts.md) `V1 구현 현황 (M4-2 fleet·publish, 2026-11-04)` 절의 publish 항목. gen 3은 게시되지 않았다.
- #17(merged `e91df84`, 측정 아님): 도구를 `@af99c61`로 올리고 gen 4를 r4(2026.11.0) 기준선으로, project를 `change-me`(acme-test v1)로 교체. 코디네이터 스크립트 실수 2건(짧은 sha ref, `control_api` 줄 중복으로 workflow 파싱 실패)을 같은 PR에서 고쳤다. publish success로 744,806,400 B 번들을 실제 Caddy TLS로 게시해 수정 r3를 실기동으로 확인했고, VM g2→g4 succeeded(02:47:54→02:48:05).
- #18(gen 5) **중단**: 코디네이터 PC 메모리 부족으로 측정 스크립트가 강제 종료됐고 check-gen job이 4분 넘게 queued였다. merge하지 않고 닫았다(gen 5 미게시).
- 실측은 단일 PC(Hyper-V VM 4 GB + WSL 중앙 + self-hosted runner)에서 했다. 운영 사이징 근거로 쓰지 않는다.

**1차 실패와 수정**: 첫 전체 실행(HEAD `b75755e`)은 38/39로 온라인 ①만 실패했다(`current_g1` false, waited 0.5 s). c0의 g1은 폐쇄망 `apply`로 적용해 중앙에 게시 번들이 없다. 그 성공 보고(seq 2)가 첫 heartbeat 다음에 도착하면 control-api `reports.py` 규칙대로 current 버전이 null이 되고, 다음 heartbeat가 manifest로 다시 채운다. 제품은 설계대로 동작했고, 하네스가 `connection == online`만 기다린 뒤 판정한 것이 결함이었다. 수정 r2 `cf296cb`에서 ① 대기 조건을 행 판정 항목 전체(`connection_online`·`heartbeat_seq`·`current_g1`)로 바꿨다. 재실행(`run.py online --keep-central` + `report`, EXIT 0)에서 ①은 11.3 s 기다려 통과했고 39/39가 됐다. 시나리오·crash 단계는 첫 전체 실행에서 모두 통과했다.
