---
id: report-mvp-gap
title: MVP Gap Audit — 첫 read-only PoC 기준 구현 격차
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - docs/sales/read-only-poc-proposal.md
  - docs/wiki/sot/product.md
  - docs/wiki/sot/testing.md
  - docs/wiki/gtm/first-contract.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/gateway.py
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/drivers
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/historian
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/alarms
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/agents
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/mes
  - docs/wiki/sot/architecture.md
last_verified: 2026-07-07
---

# MVP Gap Audit

> **legacy(PureIron_ignition) 기준** — 이 감사는 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드 기준이며 PureIron_V1 코드에는 해당하지 않는다.

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 frontmatter `last_verified` 시점의 **감사 결과**다. 그 이후의 코드·테스트 변경은 반영되지 않았을 수 있으므로, 여기 적힌 상태를 근거로 결론을 쓰기 전에 해당 코드/테스트로 재확인한다. 재감사하면 본문과 `last_verified`를 함께 갱신한다. 불변 계약은 [`docs/wiki/sot/`](../sot/agent-rules.md)에 있다.

이 문서는 **첫 read-only PoC**([read-only-poc-proposal](../../sales/read-only-poc-proposal.md))를 실제로 수행하기 위해 현재 MVP 구현에서 부족한 부분을 코드/테스트/SOT 기준으로 점검한 **gap audit**이다.

목적은 새 영업 문서를 늘리는 것이 아니라 **다음 구현 backlog를 결정**하는 것이다. 제품 의도·v1 범위 정본은 [product](../sot/product.md), 첫 계약 프레임은 [first-contract](../gtm/first-contract.md), 검증 기준은 [testing](../sot/testing.md)를 따른다. 이 문서는 그 위에서 "PoC를 돌리려면 지금 뭐가 부족한가"만 다룬다.

> 범위 제한: 고객 설비 안정성 위험(write/control, 중앙 제어, 사람 승인 없는 자동 조치)은 v1 제외로 **유지**한다([product](../sot/product.md) MVP 제외). 장기 플랫폼 기능(Ontology/Graph, 예측 모델, 고급 Twin, MES 전체)은 backlog로 낮춘다.

## 상태 분류 기준

| 상태 | 의미 |
|---|---|
| `implemented` | 핵심 경로가 코드+테스트로 존재하고 PoC에서 시연 가능 |
| `partial` | 코드는 있으나 PoC 조건(DB/키/현장 검증/승인 범위 등)에서 빠진 고리가 있음 |
| `missing` | PoC 흐름에 필요한데 backend/제품 기능으로 존재하지 않음 |
| `unknown` | 코드 존재는 확인했으나 PoC 수준 동작을 이 audit에서 확정하지 못함 |

## 흐름별 현재 상태

read-only PoC 핵심 흐름 8개([read-only-poc-proposal](../../sales/read-only-poc-proposal.md) §5) 기준.

| # | 흐름 | 상태 | 근거(코드/테스트) | 남은 격차 |
|---|---|---|---|---|
| 1 | Device/driver 연결(read-only) | `partial` | 6개 드라이버(`drivers/{opcua,modbus,s7,ls,ab,melsec}_client.py`) poll 루프·`DriverHealth`·`/api/devices` CRUD, `gateway._setup_drivers`가 DB devices 행으로 `driver.start()`. 전역 read-only latch `safety.py`(`PUREIRON_POC_READ_ONLY_MODE`)가 `DeviceTagProvider.write`에서 드라이버 write를 하드 차단. tests: `test_opcua_driver`·`test_modbus_driver`·`test_driver_health*`·`test_poc_read_only_mode` | 실드라이버는 `db_pool` 있을 때만 기동(무DB → 현장 수집 없음). s7/ls/ab/melsec는 현장 read 검증 근거 얕음 |
| 2 | 승인 태그 수집 + historian/trend | `partial` | `HistorianWriter`가 `tag.changed.*` 구독→`tag_history` batch insert, `historian/reader.py`+`/api/historian`, `TrendManagement.tsx`, `test_historian`. 승인 수집 계약: [approved tag collection contract](../sot/data-contracts.md#approved-tag-collection-contract) + `tags/collection_policy.py`(excluded>approved>default-deny, authored vs driver-derived), gateway 배선 `_setup_drivers`가 `configs/tags/approved_paths.json`→모든 드라이버 `approved_tag_paths`, tests `test_collection_policy_contract`·`test_approved_tag_collection_harness`·`test_gateway_approved_paths_wiring`·`test_driver_approved_collection`. DB-present pre-field gate는 `test_db_poc_smoke.py`가 historian 기록/조회 + alarm journal 영속을 한 smoke로 고정(`PUREIRON_DATABASE_URL` 없으면 skip) | DB(Timescale) 없으면 503·미영속(AC-7). gateway/driver allowlist 배선은 OPC-UA·Modbus·S7·LS·AB·MELSEC 모두 완료. 2주 PoC retention/조회창은 `027_add_tag_history_retention.sql`(30일 drop-after)+downsample 조회로 고정([data-contracts](../sot/data-contracts.md#historian-retention--query-window)); Trend 화면 CSV export로 리뷰용 산출물은 생성 가능. 현장 전 DB-present smoke는 고정됐고, 남은 격차는 실제 고객 DB+다중 드라이버 e2e |
| 3 | 대시보드 주요 상태 확인 | `implemented` | `dashboard/router.py`+`DashboardGrid`/widgets, WS 250ms batch, process/SCADA 화면, `AlarmSummary.tsx` 모바일 ack/note card, `test_user_dashboard`·`test_alarm_summary_ui_contract` | 모바일 알람 확인의 실제 브라우저 스크린샷/현장 증거는 PoC 전 수동 smoke 실행 시점에 남김 |
| 4 | 알람 발생/확인/조치 이력 | `partial` | `alarms/engine.py`(발생), ack 전이(AC-5/AC-6), `alarms/journal.py`가 operator·note 포함 전이를 `alarm_journal`에 영속, `AlarmSummary` active row의 조치 note 저장 UI와 History의 operator/note 표시, tests `test_alarm_ack_note_history_contract`·`test_alarm_summary_ui_contract`, PoC 전 수동 증거 절차는 [PoC browser smoke runbook](../../runbooks/poc-browser-smoke.md) | journal은 `db_pool` 없으면 조용히 no-op(무DB → 조치 이력 미영속). 실제 고객/PoC 브라우저 스크린샷은 PoC 전 실행 시점에 남겨야 함 |
| 5 | Agent troubleshooting | `partial` | `agents/`(supervisor + tag/alarm/history/health/logs agent), tools `read_tag`/`get_active_alarms`/`query_history`, 구조 검증 AC-12·AC-18c. read-only smoke `test_agent_troubleshooting_smoke.py`가 결정론적 chat model seam(외부 키 불필요)으로 읽기 grounding + write 도구 하드 차단을 고정 | 실제 provider/키에서의 답변 품질은 [agent troubleshooting answer quality runbook](../../runbooks/agent-troubleshooting-answer-quality.md)으로 수동 평가 경로를 고정했다. 실제 고객/PoC 환경에서 AQ-1~AQ-6 실행과 sanitization 증거 확보는 여전히 PoC 실행 시점 작업이다. 온프레미스 provider/키 취급은 [runtime-config](../sot/runtime-config.md)에 문서화됨 |
| 6 | 사용자/권한/감사 최소 구성 | `implemented` | JWT+refresh(AC-1), 보호 401(AC-2), RBAC 4역할+write RBAC(AC-10), `auth/audit.py`·`auth/audit_utils.py`·`user_repository.py`, `AccountsTab`/`SecurityTab`. `test_auth.py`가 login/profile/password/admin 계정 변경의 감사 로그 기록·비밀번호 평문 비노출·audit sink 실패 격리를 고정하고, `test_logs_router.py`·`test_log_triage.py`·`test_user_dashboard.py`·`test_user_settings.py`가 로그/triage/dashboard/settings 운영 경로의 safe audit 경계를 고정 | 최소 구성 충족. 감사 sink 장애는 핵심 auth/profile/password 및 운영 보조 요청을 실패시키지 않는 warn-and-continue 경계로 격리된다. v1은 서버 session store/token blacklist 없이 JWT 만료 기반 active session policy를 명시한다 |
| 7 | 온프레미스 우선 배포/환경 | `partial` | `docker/docker-compose.yml`·`docker-compose.dev.yml`·`Dockerfile.*`·`nginx.conf`, `config.py` env. PoC 전용 `docker/docker-compose.poc.yml`(timescaledb+redis+backend+frontend, simulator 없음·DB/Redis host port 미공개)+`.env.poc.example`+[on-prem read-only PoC runbook](../../runbooks/on-prem-read-only-poc.md)(구성/배포/검증/백업/롤백/handoff·수집망 분리·retention §8) | 오프라인 설치 번들 자동화는 runbook non-goal로 post-P0 유보(§2). 실제 고객 호스트 현장 검증은 유료 PoC 착수 시 |
| 8 | demo/simulator + 3D Twin + Work Order | `partial` | Simulator(`CoffeeTankProcessSimulator`/`TagSimulator`) 자동 기동, 3D Twin(`components/twin/*`·`models/router.py`·tag binding AC-16/17), 데모 spine AC-18 | **Work Order는 backend 기능 아님**(`missing`, v1 제외 유지) — [work-order-demo-handoff 런북](../../runbooks/work-order-demo-handoff.md) + [testing](../sot/testing.md) §5 AC-18로 **수동 handoff 데모 경로/체크리스트 고정**(알람 ack·note·이력 → 수동 작업 지시 handoff). `mes/`는 recipe/plan/task이지 work order 아님. 3D Twin 시각 highlight·Agent 답변 품질은 수동 검증 |

## 교차(cross-cutting) 격차

흐름 여러 개를 동시에 막던 항목. **read-only PoC-safe 모드 부재는 해결**(아래 1)됐고, 남은 무거운 잔여는 DB 존재 경로의 현장 검증과 실 LLM 답변 품질이다.

1. ✅ **read-only PoC-safe 모드 — 해결(2026-07-04).** 제안서의 "쓰기/제어 명령 미포함"([read-only-poc-proposal](../../sales/read-only-poc-proposal.md) §5·§7)이 이제 관례가 아니라 정책 강제다. 전역 latch `PUREIRON_POC_READ_ONLY_MODE`(`config.py`→`safety.py`)가 드라이버 write(`DeviceTagProvider.write`)·REST tag write(403)·agent control/schedule tool·approved-action executor를 하드 차단하고 `/api/health`가 `poc_read_only_mode`를 노출한다. 내부 수집(driver poll/simulator `write_value`)·읽기 경로는 그대로 동작한다. `test_poc_read_only_mode.py`가 회귀를 고정한다.
2. **DB 존재 경로 현장 검증(잔여).** 실드라이버 기동·historian 영속·alarm journal이 `db_pool` 없으면 조용히 축소된다. [on-prem runbook](../../runbooks/on-prem-read-only-poc.md)+`docker-compose.poc.yml`이 timescaledb 포함 경로를 문서화·검증 가능하게 만들었고, `test_db_poc_smoke.py`가 `PUREIRON_DATABASE_URL`이 있는 환경에서 historian 기록/조회와 alarm journal operator/note 영속을 한 pre-field smoke로 고정한다. 남은 것은 실제 고객 DB+다중 드라이버 end-to-end 현장 검증이다(CI는 DB env 없으면 skip).
3. ✅ **실 LLM 답변 품질 평가 경로 — runbook 고정(2026-07-07).** troubleshooting 읽기 grounding·write 차단은 키 없는 결정론 seam(`test_agent_troubleshooting_smoke.py`)으로 검증되고, 실제 provider/키가 있을 때의 답변 품질은 [agent troubleshooting answer quality runbook](../../runbooks/agent-troubleshooting-answer-quality.md)의 AQ-1~AQ-6 질문 세트·pass/fail rubric·증거 sanitization으로 수동 검증한다. 온프레미스 provider/키 취급은 [runtime-config](../sot/runtime-config.md)에 문서화됐다. 남은 일은 실제 고객/PoC 환경에서 runbook을 실행하고 sanitized evidence를 남기는 것이다.

## 다음 구현 우선순위 (issue 후보)

첫 계약 가능성·설비 안정성 기준으로 정렬. `[P0]`가 PoC 착수를 직접 막는다. 완료 항목은 traceability를 위해 ✅로 남긴다.

1. ✅ **[P0] read-only PoC-safe 모드 flag — 완료(2026-07-04).** `PUREIRON_POC_READ_ONLY_MODE` 전역 latch(`config.py`→`safety.py`)가 드라이버 write·REST tag write(403)·agent control/schedule tool·approved-action executor를 하드 차단하고 `/api/health`가 `poc_read_only_mode`를 노출한다. 제안서의 "쓰기/제어 미포함"이 관례가 아니라 정책 강제가 됐다. `test_poc_read_only_mode.py`가 회귀를 고정. env 계약은 [runtime-config](../sot/runtime-config.md).
2. ✅ **[P0] 온프레미스 PoC 배포 runbook — 완료(2026-07-04).** read-only PoC용 검증 가능한 compose 프로파일 `docker/docker-compose.poc.yml`(timescaledb+redis+backend+frontend, simulator 없음·DB/Redis host port 미공개), `.env.poc.example`, 수집망 분리·검증·백업·롤백·handoff를 [on-prem read-only PoC runbook](../../runbooks/on-prem-read-only-poc.md)에 고정. 남은 격차: 오프라인 설치 번들 자동화는 runbook non-goal로 post-P0 유보(§2), 실제 고객 호스트 현장 검증은 유료 PoC 착수 시.
3. **[P1] 승인 태그 현장 수집 검증 harness — 계약/harness/전체 드라이버 allowlist 배선 + DB-present pre-field smoke 완료(2026-07-10).** 수집 대상 whitelist/승인 개념을 제외목록 위에 얹는 [approved tag collection contract](../sot/data-contracts.md#approved-tag-collection-contract)를 SOT에 고정하고, 코드 정본 `tags/collection_policy.py`(excluded>approved>default-deny, authored vs driver-derived)와 순수 계약 test(`test_collection_policy_contract`) + modbus simulator→store→historian harness(`test_approved_tag_collection_harness`)를 추가했다. `ScadaGateway._setup_drivers()`가 startup에서 `configs/tags/approved_paths.json`를 한 번 읽어 OPC-UA·Modbus·S7·LS·AB·MELSEC driver `approved_tag_paths`로 전달하며(파일 없음→`None`, 빈 list→deny-all), `test_gateway_approved_paths_wiring`·`test_driver_approved_collection`이 gateway 배선과 각 드라이버 materialize/poll fallback 필터를 고정한다. `test_db_poc_smoke.py`는 `PUREIRON_DATABASE_URL`이 있는 환경에서 historian 기록/조회와 alarm journal 영속을 한 smoke로 검증한다. 남은 격차: 실제 고객 DB+다중 드라이버 end-to-end 현장 검증.
4. ✅ **[P1] 알람→ack→조치 note→이력 end-to-end — 계약/수동 smoke 경로 완료(2026-07-07).** backend `AlarmEngine.acknowledge`/`add_note`/`alarm_journal` 계약은 `test_alarm_ack_note_history_contract`가 고정하고, frontend `AlarmSummary` active row에서 조치 note를 `/api/alarms/{name}/notes`로 저장하며 History 화면에서 operator/note를 드러내도록 보강했다. `test_alarm_summary_ui_contract`가 UI/API 문자열 계약을 회귀 방지한다. PoC 전 브라우저 수동 smoke와 sanitized screenshot/CSV/notes 증거 절차는 [PoC browser smoke runbook](../../runbooks/poc-browser-smoke.md)에 고정했다. 남은 것은 실제 고객/PoC 환경에서 해당 런북을 실행해 증거를 남기는 일이다.
5. ✅ **[P1] Agent troubleshooting PoC 검증 경로 — smoke + 수동 품질 runbook 완료(2026-07-07).** 외부 키 없는 결정론적 chat model seam으로 알람 상황에서 read-only 도구(device health)로 답을 grounding하고 write/control 도구는 하드 차단됨을 `test_agent_troubleshooting_smoke.py`가 고정한다. 실제 provider/키에서의 답변 품질은 [agent troubleshooting answer quality runbook](../../runbooks/agent-troubleshooting-answer-quality.md)으로 질문 세트(AQ-1~AQ-6), pass/fail rubric, secret/customer-data sanitization 증거 기준을 고정했다. 필요한 provider/키의 온프레미스 취급은 [runtime-config](../sot/runtime-config.md)에 문서화. 남은 일은 실제 고객/PoC 환경에서 runbook을 실행하는 것이다.
6. ✅ **[P2] Historian retention/query window + Trend export — 완료(2026-07-07).** 2주 PoC에 맞는 retention/조회창을 `027_add_tag_history_retention.sql`(30일 drop-after, 멱등)+요청 창별 downsample(`historian/reader.query_aggregated`)로 고정하고 [data-contracts](../sot/data-contracts.md#historian-retention--query-window)에 계약화, 조정 SQL은 [runbook](../../runbooks/on-prem-read-only-poc.md) §8. Trend Management의 Download Data가 현재 선택 태그·조회 기간·시계열 값·pen statistics를 UTF-8 BOM CSV로 내보내 PoC 결과 리뷰 산출물로 쓸 수 있다.
7. ✅ **[P2] Work Order 데모 경로 명확화(backend는 v1 제외 유지) — 완료(2026-07-03).** backend Work Order는 v1 밖으로 유지([testing](../sot/testing.md))하고, 영업 데모가 반복 가능하도록 [work-order-demo-handoff 런북](../../runbooks/work-order-demo-handoff.md)에 수동 handoff 스크립트·검증 체크리스트를 고정했다(알람 ack·조치 note·이력 → Agent troubleshooting → 수동 작업 지시 handoff). SOT/testing/product 문구를 "backend 기능"이 아니라 "demo/manual handoff"로 정렬. 다음 단계: backend Work Order API가 생기면 AC-18 자동화 표로 승격(현재는 v1 제외 유지). 범위 확대 아님.
8. **[P3] 멀티 드라이버 현장 준비도.** s7/ls/ab/melsec 통합 검증 확대는 실제 고객 PLC가 정해진 뒤로 미룬다(`needs-founder-input`).
9. ✅ **[베이스 강화] Core domain model boundary 고정 — 완료(2026-07-04).** 단기 영업 흐름에 코어 모델이 흐려지지 않도록 Tag(정의≠런타임 값)·Device/DriverHealth·Alarm/AlarmJournal·Historian(append/query)·Agent(read-only projection)·Work Order/MES-lite 경계를 [architecture](../sot/architecture.md#core-domain-model-boundary-sot) 한 절에 SoT로 고정하고, `backend/tests/test_core_domain_boundary_contract.py`가 문구·금지 패턴(agent read-only, device health가 태그로 새지 않음)으로 silent drift를 막는다. 새 기능/스키마 추가가 아니라 기존 경계 명문화다.

## 다음 후보 backlog (잔여만)

위 표에서 ✅가 아닌, 첫 read-only PoC를 실제로 굴릴 때 남은 후보다. PoC-safe 모드·온프레미스 runbook·retention은 이미 완료됐으므로 착수 자체를 막는 P0는 없다.

1. **실제 고객 DB+다중 드라이버 e2e 현장 검증(교차 격차 2 + #3 잔여).** `docker-compose.poc.yml` 경로와 `test_db_poc_smoke.py` pre-field smoke는 고정됐으므로, 유료 PoC 착수 시 실제 고객 호스트/DB/드라이버 조합에서 수집·영속·조회 증거를 남긴다.
2. **실 LLM 답변 품질 runbook 실행(#5 잔여).** 평가 경로는 고정됐고, provider/키가 있는 실제 PoC 환경에서 AQ-1~AQ-6 답변 품질과 sanitized evidence를 확인해야 한다.
3. **멀티 드라이버 현장 준비도(#8, `needs-founder-input`).** s7/ls/ab/melsec 통합 검증 확대는 실제 고객 PLC 확정 후.

## v1에서 유지되는 제외 (backlog 낮춤)

audit 결과와 무관하게 v1 제외로 **유지**한다. 근거는 [product](../sot/product.md) MVP 제외와 [agent-rules](../sot/agent-rules.md).

- 설비 write/control, 사람 승인 없는 자동 조치, 중앙 제어.
- 예측 모델 기반 이상 감지, 완전 예측 AI Simulation.
- Ontology/Graph 원인 추론, 고급 Digital Twin 완성도.
- 완전한 MES, 범용 no-code config builder.

## 검증

이 문서는 SOT/문서 변경이므로 아래를 실행한다.

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```
