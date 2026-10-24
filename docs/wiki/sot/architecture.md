---
id: sot-architecture
title: Architecture SOT — 시스템 구조 (V1)
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - services/core/src/pureiron_core/runtime.py
  - services/core/src/pureiron_core/app.py
  - services/core/src/pureiron_core/cli.py
  - services/core/.importlinter
  - services/core/src/pureiron_core/acquisition/server.py
  - services/core/src/pureiron_core/acquisition/ingest.py
  - services/core/src/pureiron_core/platform/bus.py
  - services/core/src/pureiron_core/tags/ports.py
  - services/collector/src/pureiron_collector/stream.py
  - services/updater/src/pureiron_updater/phases.py
  - packages/contracts/collector.proto
  - deploy/compose/site.compose.yml
  - deploy/releases/r3.json
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/wiki/sot/architecture.md
last_verified: 2026-10-17
---

# Architecture SOT

> **PureIron_V1 기준** — 이 문서는 Foundation 재설계(ADR-019~028)로 만든 V1 구조와 현재 구현 상태의 정본이다.
> legacy(`PureIron_ignition`) 모놀리식 구조는 마지막 절 [legacy 참고](#legacy-참고-pureiron_ignition)에 요약만 두고,
> 전체 설명은 [legacy-final의 architecture.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/wiki/sot/architecture.md)를 본다.

PureIron V1은 현장 한 곳에 설치되는 **서비스 4개(collector·core·web·site-db) + 호스트 Updater**와, 벤더 쪽 **중앙
도구(control-api·fleet repo)**로 이루어진다. 현장은 중앙·AI 없이도 모니터링·알람·이력이 동작하고, 현장 변경은
서명된 번들(`.pib`)로만 들어온다.

제품 의도와 v1 범위는 [product](product.md), 불변 작업 규칙은 [agent-rules](agent-rules.md), 코드 작성 규약은
[coding-style](coding-style.md), 결정 근거는 [ADR](../adr/index.md), DB·마이그레이션·release 계약은
[data-contracts](data-contracts.md)를 따른다.

## 이 문서의 원천

- `services/core/src/pureiron_core/runtime.py` — core 유일한 조립 지점(`SiteRuntime.build`)
- `services/core/.importlinter` — core 모듈 경계 계약 (a)(b)(c)
- `packages/contracts/collector.proto` — collector ↔ core 계약
- `services/updater/src/pureiron_updater/phases.py` — 현장 적용 상태기계
- `deploy/compose/site.compose.yml`, `deploy/releases/*.json` — 현장 구성과 release 기록

## 아키텍처 경계

- PLC 로직을 수정하지 않는다. 기존 PLC 주소 쓰기는 ADR-013(개정) 가드레일 아래에서만 한다(기본 읽기 전용, 쓰기
  경로는 M3c).
- PureIron은 관제·알람·이력·트렌드·감사와 Agent 보조 계층을 제공한다. 사람 승인 없는 자동 조치는 v1 범위가 아니다.
- **graceful degradation**: `ai` 서비스와 중앙(control-api)은 선택이다. 없어도 현장은 동작한다. collector가
  죽으면 태그는 BAD/DISCONNECTED가 되고, site-db는 필수다. core `/readyz`는 DB 연결 + schema epoch envelope +
  collector 세션이 모두 있을 때만 ready다(수집·이력 상태는 정보로만 낸다).
- 현장 변경 경로는 서명 번들 하나뿐이다. 검증에 실패하면 서비스를 멈추기 전에 거부한다. registry pull은 하지 않는다.

## 시스템 구성 (ADR-019)

| 구성 | 역할 | 구현 상태 (2026-10-17) |
|---|---|---|
| `collector` | 장비 통신, `ValueBatch`·`DeviceHealth` 송신. 무상태·DB 접근 없음, 60s 메모리 링버퍼 | Modbus TCP 드라이버·링버퍼·replay·DeviceHealth (M3a) |
| `core` | 태그·이력·장비 상태·프로젝트 적용 + 알람·인증·감사·실시간(M3b), REST `/api/v1`, gRPC 서버 | acquisition·tags·historian·project (M3a), 알람·인증·감사·WS·`/api/v1/devices` (M3b) |
| `web` | nginx + React(로그인 + 태그 현황·알람/ACK·트렌드·장비 상태·감사 로그) | 로그인 + 5화면, WS 실시간, Playwright 스모크 (M3b) |
| `site-db` | TimescaleDB PG16(`pureiron/site-db:2.30.2-pg16`) | 마이그레이션 `0001~0012`, epoch 4 (r3) |
| `ai` | core read-only API 소비 | 계약만, 첫 완료 범위 제외 |
| Updater | 호스트 systemd 서비스(컨테이너 아님): 번들 검증·백업·마이그레이션·전환·롤백 | 완료(M2), VM 실측 30/30(r0→r1, r1→r2, r2→r3) |
| control-api | 벤더 내부 중앙 API: 등록·heartbeat·desired·reports·drafts·fleet | `GET /api/v1/healthz` 골격만(M4) |
| control-web | 중앙 읽기 전용 표(basic auth) | 미착수(M4) |
| `simulator` | 개발용 Modbus 시뮬레이터(compose profile `dev`) | `deploy/simulator/` |

포트: 호스트에 노출되는 것은 web `${PUREIRON_WEB_PORT:-80}` → 컨테이너 `8080`뿐이고, nginx가 `/api/`를 `core:8000`으로
프록시한다. core HTTP `:8000`과 gRPC `:50051`(collector가 `PUREIRON_CORE_GRPC=core:50051`로 접속)은 compose 내부 포트다.

저장소 레이아웃(ADR-026): `services/{core,collector,web,updater,control-api,ai}` · `packages/{contracts,bundle-tools,project-tools}`(계약·공용 도구) · `deploy/`(compose·install·build·bundle·release 기록) · `tests/e2e/`(Hyper-V VM 실측). 고객 프로젝트는 고객별 repo에 둔다.

## 통신 경계 (ADR-020)

| 채널 | 경로 | 용도 | 상태 |
|---|---|---|---|
| gRPC 양방향 스트림 | `Acquisition.Session`(core가 서버) | collector → `Hello`·`ValueBatch`·`DeviceHealth`, core → `AcquisitionPlan`·`Ack` | M3a |
| REST | `/api/v1/*` | `healthz`·`readyz`, `project`(edit lock) → M3b에서 history·alarms·auth·audit·devices | 일부 |
| WebSocket | `/api/v1/ws` | 태그 값·알람·장비 상태 푸시(PD-10, 메시지 계약은 M3b에서 확정) | M3b |
| 현장 → 중앙 | HTTPS polling(현장이 먼저 연결) | desired 조회·보고·드래프트 업로드 | M4 |

- **Ack 의미론**: core는 batch를 in-memory ingest(현재값 반영 + historian 버퍼 적재) 직후 Ack 한다. collector 링버퍼는
  core 다운·단절 구간만, DB 장애는 core historian 상한 버퍼(600,000행 + 쓰는 중 최대 20,000행)가 담당한다.
- **replay**: 재연결 뒤 링버퍼에서 다시 보낸 `replay=true` batch는 이력에만 기록한다(현재값·알람·WS 갱신 없음, deadband 우회).
  중복은 `UNIQUE(tag_id, time)`로 흡수한다.
- **단절·무신호**: collector 세션이 모두 끊기면 전 장비 DISCONNECTED·전 태그 BAD. 세션이 있어도 장비별
  `max(3×poll_ms, 5s)` 동안 신호가 없으면 그 장비 태그는 BAD, 장비는 DISCONNECTED.
- 프로토콜 추가는 proto `TagPoint.address` oneof 확장으로만 한다(호환성 diff 게이트, ADR-028).

## core 구조

```
cli.py ──▶ runtime.SiteRuntime.build(settings) ──▶ app.create_app(rt)
              │ 생성자 주입
              ├─ platform: Database · EventBus · migrate(Catalog, SchemaGuard)
              ├─ tags: TagStore
              ├─ acquisition: AcquisitionServer(gRPC)
              │     ├─ AcquisitionService ─ Ingestor · DeviceMonitor
              │     └─ PlanKeeper ─ PgDefinitionsSource(정의 → plan)
              ├─ historian: Historian ─ Recorder ─ HistoryWriter ─ PgRowStore
              ├─ project: PgProjectStateStore(project_state·edit lock)
              └─ writes: WritesService ─ policy(선언·검사·판정) ─▶ AcquisitionService(WriteChannel)
```

- 전역 싱글톤·`app.state`·service locator가 없다. 모든 객체는 `SiteRuntime.build`에서 만들어 생성자로 넘긴다.
- import 경계(import-linter): `api > domain > platform` 계층, 도메인 간은 `<module>.ports`만, `runtime`·`app`은 진입점만
  import한다. 모듈 내부 구성·추상화 기준은 [coding-style](coding-style.md) 3·4절.
- CLI `pureiron-core`: `serve`(HTTP + gRPC), `migrate status|up|verify|lint`, `project apply|export|restore`(M5 본구현),
  `apply-definitions <project dir>`(태그·장비 정의 upsert).

| 모듈 | 책임 | 상태 |
|---|---|---|
| `platform` | asyncpg `Database`(획득·반환 제한), 내부 `EventBus`, 마이그레이션 catalog·lint·`SchemaGuard` | M1~M3a |
| `acquisition` | collector 세션, plan 배포·갱신, batch ingest, 장비 무신호 판정 | M3a |
| `tags` | 현재값 `TagStore`(in-memory), 품질 | M3a |
| `historian` | recorder(deadband·max_interval, replay 우회) → writer(상한 버퍼·1s flush) → `tag_history`, reader(raw/1m/1h) | M3a(HTTP 노출은 M3b) |
| `project` | `project_state`·edit lock, 정의 소스(`definitions_rev`) | 골격 + 정의(M3a), 정식 apply는 M5 |
| `api` | `build_<x>_router` + pydantic 모델 | health·project |
| `alarms` | 4상태 알람 엔진(`tag.changed.*` 동기 평가)·저널(상한 큐 + writer)·ACK 트랜잭션·기동 복원 | M3b-2 |
| `auth`·`audit` | 세션 인증·역할, 감사 기록·조회 | M3b-1 |
| `realtime` | WS `/api/v1/ws` 연결·구독·태그 합치기·알람/장비 방송 | M3b-2 |
| 장비 상태 | 새 도메인 없이 `acquisition.ports` `DeviceSnapshot`·`DeviceStatusSource`(`DeviceMonitor`) → `GET /devices` | M3b-2 |
| `writes` | PLC 쓰기: 선언 해석·실행 전 검사·2단계 확인(prepare→confirm)·감사 의도/결과 행·readback 판정. 전송은 `acquisition.ports.WriteChannel` | M3c-2 |

## Core Domain Model Boundary (SoT)

각 도메인의 단일 진실 소스(SoT)는 하나다. 이 경계 문장은 silent drift를 막는 불변이다.

| 도메인 | 단일 진실 소스 | 경계 문장(불변) |
|---|---|---|
| Tag | 정의 = 고객 project repo → site-db `tags.definition`(`apply-definitions`), 값 = core `TagStore` in-memory | **Tag definition != runtime value** |
| Device | 연결·건강 = collector `DeviceHealth` → core `DeviceMonitor` → `/api/v1/devices`(M3b) | **DeviceHealth is not a tag** |
| Alarm (M3b) | 현재 상태 = core 알람 엔진, 이력·감사 = 알람 저널 | **AlarmJournal is audit/history not current state** |
| Historian | `tag_history` hypertable + CAGG `tag_history_1m`·`_1h`(epoch 3부터 실시간 집계) | **Historian is append/query path** — 현재값 소스가 아니다 |
| Project | 고객 project repo(Git), 현장은 적용본 + 드래프트(단조 `site_rev`, ADR-025) | 고객별 코드 분기 없음, 비밀값은 `secret://`만 |
| Agent Context | core read-only API projection(`ai` 서비스) | **Agent is read-only/context projection** |
| Work Order / MES-lite | 미래 business workflow 경계(backend 기능 아님) | tag/alarm의 hidden mutation이 아니다 |

## 데이터 흐름: 수집 → 현재값·이력

```text
PLC ─Modbus TCP─▶ collector poller ─▶ ValueBatch(100ms 또는 500값) ─▶ 링버퍼(Ack 전 보관)
   ─gRPC Session─▶ core AcquisitionService
        ① Ingestor
           ├─ 이력: Recorder(fresh live는 deadband·max_interval, replay·오래된/미래 live는 원시 기록) ─▶ HistoryWriter 버퍼
           └─ 현재값: fresh live만 TagStore.commit() + DeviceMonitor.seen() (동기, 이벤트 목록 반환)
        ② Ack(seq) ─▶ collector 링버퍼에서 제거          ← 메모리 반영 직후, 발행 전
        ③ 락 밖에서 EventBus.publish(tag.changed.<path>, device.changed.<id>) ─▶ (M3b) 알람·WS
HistoryWriter ─1s flush, INSERT … ON CONFLICT DO NOTHING─▶ site-db tag_history ─▶ CAGG 1m/1h
```

- Recorder는 일부 샘플을 거부한다(plan_rev가 앞선 batch, 현재 정의에 없는 tag_id의 live 샘플, int4 범위 밖 tag_id → `rejected_total`).
  상세 규칙은 [data-contracts](data-contracts.md)의 M3a 절.
- 이력 기록은 `tag.changed` 구독이 아니라 Ingestor가 직접 넘기는 sink다(이벤트 루프의 reader 경로에 DB I/O가 없다).
  M3b-2 결정(D8): 별도 publisher task를 두지 않는다. 대신 **bus 핸들러는 I/O를 await 하지 않는다** — 알람 엔진은
  동기 평가 후 저널 행을 상한 큐에 넣고(writer task가 비움) 전이 이벤트를 돌려주며, WS는 연결별 메모리(태그 합치기·
  상한 이벤트 큐)에만 넣는다. 그래서 느린 DB·클라이언트가 ingest 루프를 붙잡지 않는다.

## 쓰기 경로 (ADR-013 개정, M3c)

```text
web·스크립트 ─POST /writes(prepare)─▶ writes: 검사만(D9) → write_id·만료(TTL 60s)   ← 아무것도 안 보냄
            ─POST /writes/{id}/confirm─▶ writes.confirm
   ① 준비 표에서 원자적 pop(본인·TTL) + 같은 태그 진행 중 표 예약(동기)
   ② edit-lock 조회(site-db, 제한 시간) → 동기 구간에서 D9 검사(TagStore·수집 스냅샷) ── 실패: 409 + tag.write_rejected
   ③ 감사 의도 행 tag.write(outcome SENDING) 커밋 ── 실패: 503, 안 보냄
   ④ 동기 재검사 → AcquisitionService.send: 세션 1개로 WriteRequest{request_id 새 값, plan_rev, timeout_ms}
        collector: 2중 방어 → 장비 poller 큐(poll 사이) → FC5/6/16 → 재읽기 → WriteResult
   ⑤ 판정(CONFIRMED·WRITTEN·MISMATCH·FAILED·REJECTED·NO_RESPONSE) → 결과 행 tag.write(같은 write_id, best-effort) → 200
```

- 쓰기는 core만 보낸다. 현장 스위치(`PUREIRON_WRITES_ENABLED`, 기본 꺼짐)가 꺼지면 plan `TagPoint.writable`은 전부 false고
  모든 쓰기가 검사 `writes_enabled`로 거부된다(collector도 `writable=false`를 거부 — 2중 방어).
- 쓰기 선언은 고객 project repo의 태그 `write` 블록이 정본이다(`tags.definition`에 원문 저장 — migration 없음). core
  `writes/policy.py`가 다시 읽고 어긋나면 그 태그만 쓰기 불가로 둔다. plan `writable`은 PlanKeeper가 writes에 물어 정한다.
- 검사는 TagStore 동기 스냅샷만 보고(Lock discipline), DB(edit-lock)는 그 앞에서 await 한다. 의도 행을 쓰는 동안
  상태가 바뀔 수 있어 보내기 직전에 동기로 다시 본다.
- 응답 대기 시한 = `PUREIRON_WRITE_TIMEOUT_S`(W), collector 보내기 기한 `timeout_ms` = W − 2s − 2 × 장비 timeout_ms.
  core는 collector가 현재 plan_rev로 batch를 보고한 뒤에만 보내므로(plan 적용 중 수신 정지 창 차단), core가
  NO_RESPONSE를 확정한 뒤에는 그 쓰기가 PLC에 닿지 않는다. 시한 초과·세션 종료·collector `INDETERMINATE`·모르는
  status는 NO_RESPONSE("결과 모름, 현장 확인")다. 자동 재시도는 없다. 상세 계약은 [api-events](api-events.md) V1 쓰기
  API 절, collector 쪽은 [data-contracts](data-contracts.md) M3c-1 절.

## 이벤트 버스 · 잠금 규율

core 내부 `platform/bus.py` `EventBus`(ADR-010 유지 — core 프로세스 내부 범위)만 있다. 서비스 간 pub/sub은 없다.
legacy와 달리 V1에는 `tag.changed.all` 토픽이 없고, historian은 이벤트를 구독하지 않는다(위 데이터 흐름의 sink).

| 토픽 | 발행 | 내용 |
|---|---|---|
| `tag.changed.<path>` | acquisition(TagStore 결과) | 값·품질이 바뀐 태그 |
| `device.changed.<device_id>` | acquisition `DeviceMonitor` | 장비 연결 상태 변화 |
| `collector.session.changed` | acquisition server | collector 세션 수 |
| `alarm.changed.<alarm_id>` | alarms(M3b-2, `tag.changed` 핸들러가 돌려준 전이·타이머·정의 변경·ACK) | 전이 뒤 `AlarmSnapshot` |

- 구독은 fnmatch 패턴(`tag.changed.*`). 핸들러 예외는 기록만 하고 다른 구독자 전달을 막지 않는다.
- **핸들러 규칙(M3b-2 D8)**: `publish`는 핸들러를 순서대로 await 한다(발행자 = ingest 루프·Ack 뒤). 핸들러는 I/O를
  await 하지 않고 메모리 갱신·큐 적재만 한다. 핸들러가 내야 할 이벤트(알람 전이)는 **돌려주면** 버스가 지금
  이벤트를 모든 구독자에게 전한 뒤 이어서 발행한다(너비 우선, 순서 결정적).
- 기동 순서: db → historian → alarms(저널 복원·writer) → realtime → writes(표만, 기동 감사 task) → acquisition,
  정지는 역순(수집 중단 — 진행 중 쓰기는 세션 종료로 NO_RESPONSE → writes task 정리 → WS 1001 close → 알람 저널 최종
  쓰기 → 이력 최종 쓰기 → db).
- **잠금 규율**: TagStore를 바꾸는 메서드는 동기로 끝나고 이벤트 목록을 반환한다. DB I/O와 `publish`는 그 뒤, 바깥에서
  한다(불변 규칙 Lock discipline, [coding-style](coding-style.md) 8절).

## 지원 디바이스 프로토콜 (v1)

| 프로토콜 | V1 구현 | 비고 |
|---|---|---|
| Modbus TCP | collector `drivers/modbus.py` (M3a) | 1차 범위 |
| OPC-UA → Siemens S7 → LS Electric → Allen-Bradley → Mitsubishi MELSEC | 미이식 | legacy-final에는 6개 모두 있다. 이 순서로 collector `drivers/`에 직접 구현하고 proto oneof를 확장한다(foundation plan §9, 마일스톤 미정) |

- 프로토콜은 동적 로딩·plugin registry 없이 `drivers/`에 직접 구현한다(No plugin system).
- 장비 연결 상태는 태그가 아니라 `DeviceHealth`로만 전달한다.
- 영업 자료의 "6개 프로토콜 지원"은 legacy 기준이다. V1에서 이식이 끝나기 전까지 제안·계약 문구는 [product](product.md)와 함께 확인한다.

## DB · 마이그레이션 (ADR-021, ADR-024)

- asyncpg raw SQL만(No ORM). 스키마 변경은 `services/core/migrations/NNNN_*.sql` + `pureiron-core migrate`로만, **ADD-only**,
  부팅 시 자동 실행하지 않는다(Updater `migrating` 단계 또는 운영자가 실행).
- 마이그레이션을 포함한 release만 schema epoch를 +1 한다. 앱은 `app_expected ≤ 현재 epoch ≤ app_max_compatible`일 때만
  ready다 → 한 단계 이전 앱으로 롤백할 수 있다.
- 현재: r0 = epoch 1, r1 = epoch 2, r2 = epoch 3(`0009`), **r3 = epoch 4**(`0010~0012`). 다음 migration은 `0013~`, epoch 5.
  세부(테이블·CAGG·lint 규칙·release 기록)는 [data-contracts](data-contracts.md).

## 배포 · 중앙 (ADR-022, ADR-023)

- 배포 정본: 벤더 repo `pureiron-fleet`의 `sites/<site>.yaml`(generation 카운터). PR 승인·merge = 배포 승인 → 서명 번들
  `.pib` 게시(M4).
- 현장 Updater 상태기계: `fetched` → `verified` →(적용 창)→ `committed` → `locked` → `backed_up` → `migrating` →
  `switching` → `checking` → `succeeded`, 실패 시 `rolling_back` → `failed`(수동 개입 필요 시 `failed_manual`).
  서명·검증 실패는 커밋 전에 `rejected`로 끝난다(서비스 영향 없음, generation을 기록하지 않음).
  각 단계는 멱등이라 crash·전원 리셋 뒤 다시 실행해 수렴한다.
- 폐쇄망 현장도 같은 `.pib`를 USB 등으로 반입해 같은 검증을 거친다. 설치는 install 번들(`deploy/install/`)로 오프라인 부트스트랩.
- 이미지 신원 = config digest(release 기록 `deploy/releases/<r>.json`). 현장 이미지는 번들에서 `docker load`만 한다.

## V1 구현 현황 (2026-10-17)

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| M1 | 저장소 골격, core 조립·import 계약, G1 게이트(CI), compose | 완료 |
| M2 | Updater·bundle-tools·install 번들, r0·r1, Hyper-V VM 실측 30/30 | 완료 |
| M3a | collector(Modbus·링버퍼·replay), core tags·historian·정의, r2(epoch 3), VM 실측 r1→r2 30/30 | 완료 |
| M3b | alarms·auth·audit·realtime·devices·history API, web 로그인 + 5화면, r3(epoch 4), VM 실측 r2→r3 30/30 | 완료 |
| M3c | PLC 쓰기 경로(ADR-013 가드레일): M3c-0 REST Origin/CSRF·로그인 제한, M3c-1 계약·collector 쓰기·simulator, M3c-2 core 쓰기(writes·REST·감사) | 진행 중 — M3c-3 web 쓰기 화면 예정 |
| M4 | control-api·control-web·fleet·release 파이프라인 | 예정 |
| M5 | 프로젝트 수명주기(project-tools 완성, apply·드래프트 회수) | 예정 |

진행 기록의 정본은 각 PR과 [data-contracts](data-contracts.md)의 release·실측 절이다.

## 프론트엔드 경계

- `services/web`: React + Vite + TypeScript(strict), API 타입은 core OpenAPI에서 생성한 `src/api/schema.d.ts`만 쓴다.
- 실시간은 `/api/v1/ws`(M3b). 상태 관리·UI 라이브러리는 필요가 생긴 PR에서 결정한다.
- 작성 규약은 [coding-style](coding-style.md) 10절. legacy 화면 구조(Jotai·3D Twin·Agent SSE)는 [frontend](frontend.md)(legacy 기준).

## legacy 참고 (PureIron_ignition)

M3b 이후 이식 작업에서 legacy 의미를 확인할 때만 쓴다. 새 코드에는 적용하지 않는다. 코드는
`git -C C:/PureIron_ignition show legacy-final:<path>`로 읽는다.

- legacy는 단일 FastAPI 백엔드 + 단일 React 프론트엔드 + PostgreSQL(TimescaleDB + pgvector) + Redis 모놀리스였다. 모든 런타임
  상태를 `ScadaGateway` 싱글톤(`backend/src/pureiron/gateway.py`)이 가졌고, 마이그레이션은 부팅 시 자동 실행했다. V1은
  이 둘을 각각 `SiteRuntime` 생성자 주입과 ADD-only `migrate` 명령으로 바꿨다.
- 주요 모듈: `tags/`·`drivers/`(6개 프로토콜)·`alarms/`·`historian/`·`events/`·`ws/`·`auth/`·`chat/`·`agents/`·`rag/`·`models/`(3D)·
  `udt/`·`mes/`. V1 이식 대상과 순서는 foundation plan §9.

### TagSourceKind Taxonomy

legacy `TagDefinition.source_kind`는 DB row 소유자를 구분했다: `AUTHORED`(사용자 작성) · `UDT_DERIVED`(UDT 인스턴스 확장) ·
`DRIVER_DERIVED`(장비 자동 생성). V1의 태그 정의는 고객 project repo가 정본이고 `apply-definitions`가 path 기준으로
upsert한다(UDT는 이식 순서 2번). 상세는 [legacy-final architecture.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/wiki/sot/architecture.md#tagsourcekind-taxonomy).

### 온톨로지 투영

legacy `ontology/`는 저장소가 아니라 기존 정본(TagStore·UDT·AlarmEngine·DriverHealth)을 읽는 TTL 캐시 투영이었다(엔티티 6종,
관계 5종 고정, 근거 없는 관계는 만들지 않음). V1에서는 `ai` 서비스 이식 때 core read-only API 위에서 다시 정한다.
