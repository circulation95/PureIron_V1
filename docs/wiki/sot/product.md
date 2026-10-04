---
id: sot-product
title: Product SOT — 제품 의도
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - docs/PRD.md
  - README.md
  - USER.md
last_verified: 2026-06-30
---

# Product SOT

PureIron은 중소 제조업과 산업시설을 위한 경량 Web HMI/SCADA 플랫폼이다.

v1은 기존 PLC와 설비 제어 로직을 대체하지 않고, 그 위에 웹 기반 관제, 알람, 이력, 트렌드, 작업지시, Agent 보조 계층을 얹는 것을 목표로 한다.

장기적으로는 공장 운영 데이터를 Agent, Digital Twin, Ontology, 예측 모델과 연결하는 IIoT Agent Platform으로 확장한다.

## 이 문서의 원천

- [docs/PRD.md](../../PRD.md) — 기존 제품 목표·사용자·핵심 기능·MVP 제외·성능 요구
- [README.md](../../../README.md) — 설치/실행 진입점, 기능 요약
- [USER.md](../../../USER.md) — 사용자/팀 맥락, 장기 기술 비전, 작업 성향

## 제품 정체성

PureIron의 제품 정체성은 두 단계로 정의한다.

### v1: 경량 Web HMI/SCADA

v1은 고객이 기존 설비를 유지하면서 웹과 모바일로 설비 상태, 알람, 이력, 트렌드, 작업 지시 흐름을 확인할 수 있는 경량 Web HMI/SCADA다.

초기에는 실제 PLC 제어를 대체하지 않는다. 고객 현장에서 이미 PLC로 동작하는 프로세스 위에 관제와 운영 보조 계층을 얹는다.

### 장기: IIoT Agent Platform

장기적으로 PureIron은 단순 HMI 화면을 넘어선 IIoT Agent Platform으로 확장한다.

목표는 공장 운영 데이터를 사람이 보는 화면에만 머무르게 하지 않고, Agent와 Digital Twin, Ontology, 예측 모델이 이해하고 조치 후보를 제안할 수 있는 운영 지식 구조로 발전시키는 것이다.

## 대상 고객과 사용자

PureIron은 구매 의사결정자와 실제 사용자를 구분한다.

### 초기 구매/의사결정 고객

초기 핵심 고객은 소규모 제조업 사장, 공장장, 현장 관리자다.

이들은 대기업 자동화팀이나 전문 설비팀이 아니라, 현장의 문제를 직접 겪고 비용 대비 효과를 중요하게 보는 의사결정자다.

이들에게 중요한 질문은 다음과 같다.

- 기존 설비를 그대로 쓸 수 있는가?
- 현장 밖에서도 공장 상태를 볼 수 있는가?
- 알람을 빠르게 확인하고 조치할 수 있는가?
- 설치하다가 설비가 불안정해지지는 않는가?
- 외산 HMI/SCADA보다 비용 효율이 좋은가?
- 직원들이 어렵지 않게 쓸 수 있는가?
- 나중에 Agent, Digital Twin, 예측 모델로 확장할 수 있는가?

### 실제 사용자 역할

| 역할 | 하는 일 |
|---|---|
| 운영자(operator) | 라인 상태 모니터링, 알람 확인, 기본 조치 |
| 엔지니어(engineer) | 태그·디바이스·알람 셋포인트·설비 설정 |
| 관리자(administrator) | 사용자·권한 관리, 감사 로그 확인 |
| 분석가/뷰어(viewer/analyst) | Historian, Trend, 리포트, Agent 질의 |

RBAC 4개 역할은 admin / engineer / operator / viewer를 기본값으로 둔다. 권한 모델 상세는 [api-events](api-events.md)·[agent-rules](agent-rules.md)를 따른다.

## 고객이 겪는 문제

초기 고객은 다음 문제를 겪는다.

- 공장에 직접 가거나 현장에 전화해야 설비 상태를 알 수 있다.
- 알람이 발생해도 원인과 조치 이력이 체계적으로 남지 않는다.
- 설비 상태, 생산 현황, 알람, 트렌드, 작업 지시가 흩어져 있다.
- 외산 HMI/SCADA는 소규모 현장에 비싸고 무겁게 느껴질 수 있다.
- 기존 설비를 건드렸다가 생산 안정성이 깨질까 걱정한다.
- AI, Digital Twin, Agent는 관심은 있지만 당장 쓸 수 있는 형태로 느껴지지 않는다.

PureIron은 이 문제를 “기존 설비 위에 얹는 가벼운 웹 기반 관제·운영 계층”으로 해결한다.

## v1 핵심 가치

v1은 첫 계약 가능성과 영업 데모 설득력을 높이는 가치에 집중한다.

1. 기존 설비를 유지하면서 웹/모바일에서 공장 상태를 본다.
2. 알람을 즉시 확인하고 조치 이력을 남긴다.
3. 설비, 태그, 로그, 트렌드를 한 화면에서 확인한다.
4. 상태 머신 기반 Simulator와 3D Digital Twin을 연결해 실제 공정처럼 보이는 데모를 제공한다.
5. Agent가 Simulator, Tag, Alarm, Log, 3D Twin 맥락을 기반으로 이상 상황과 원인 후보를 설명한다.
6. Work Order 생성/승인 흐름을 데모한다.

핵심 경험은 다음이다.

> 공장에 가지 않아도 공장을 본다.
> 전화하지 않아도 상황을 안다.
> 복잡한 화면을 뒤지지 않아도 조치 흐름으로 이어진다.

## v1 핵심 기능

v1은 첫 계약 가능성과 영업 데모 설득력을 높이는 기능에 집중한다.

1. **Device / Tag 모델**
   - 설비와 태그를 구조화한다.
   - 향후 Agent, Digital Twin, Ontology의 기반이 된다.
   - v1은 OPC-UA / Modbus TCP / Siemens S7 / LS Electric / Allen-Bradley ControlLogix / Mitsubishi MELSEC 6개 프로토콜을 직접 지원한다. LS·MELSEC·AB는 한국·아시아 현장 영업 데모와 첫 계약 설득력을 위한 우선 지원 대상이다. 프로토콜 정본 표와 추가 정책은 [architecture](architecture.md#지원-디바이스-프로토콜-v1)를 따른다.

2. **실시간 상태 대시보드**
   - 설비 상태, 생산 현황, 주요 태그 값을 웹/모바일에서 확인한다.

3. **Alarm / Alarm History**
   - 알람 발생, 확인, 조치 이력을 관리한다.
   - 모바일 알람 확인을 핵심 경험으로 둔다.

4. **Trend / Historian**
   - 주요 태그의 시계열 변화를 저장하고 조회한다.
   - 원인 분석과 리포트의 기반이 된다.

5. **상태 머신 기반 Simulator**
   - 단순 랜덤 값 생성기가 아니라 실제 공정 흐름처럼 동작한다.
   - 영업 데모, 개발 테스트, 알람 시나리오 검증에 사용한다.

6. **3D Digital Twin View**
   - Simulator와 태그 상태를 3D 설비/공정 화면에 연결한다.
   - 고객이 공정 상태를 직관적으로 이해하게 만든다.

7. **Agent-assisted Troubleshooting**
   - Agent가 Tag, Alarm, Log, Trend, 3D Twin 맥락을 기반으로 이상 상황과 원인 후보를 설명한다.
   - v1에서는 완전 자동 제어가 아니라 사람의 판단을 돕는 역할로 제한한다.

8. **Work Order Demo (수동 handoff, backend 기능 아님)**
   - 이상 상황에서 조치 후보를 제안하고, 알람 ack·조치 note·이력 위에서 **사람이 기존 작업 지시/CMMS 프로세스로 넘기는 수동 handoff 흐름**을 데모한다.
   - v1은 Work Order를 backend에서 생성·저장·승인하지 않는다(DB/API/router 없음, `mes/`는 recipe/plan/task이지 work order 아님). 반복 가능한 데모 스크립트·검증 체크리스트는 [work-order-demo-handoff 런북](../../runbooks/work-order-demo-handoff.md), 자동/수동 분리는 [testing](testing.md#5-acceptance-criteria-현재-범위) AC-18을 따른다.

9. **Auth / RBAC / Audit**
   - 사용자, 권한, 감사 로그를 제공한다.
   - 현장 운영 시스템으로서 최소한의 신뢰성을 확보한다.

## Simulator / 3D Digital Twin / AI Simulation 방향

PureIron의 Simulator는 단순 랜덤 태그 생성기가 아니다.

v1 Simulator는 상태 머신 기반으로 실제 공정 흐름처럼 동작하며, 3D Digital Twin과 연결되어 설비 상태, 알람, 이상 징후를 시각적으로 보여준다.

초기 AI Simulation은 실제 설비를 자동 제어하거나 공정을 완전 예측하는 기능이 아니다. v1에서는 Simulator, Tag, Alarm, Log, 3D Twin 맥락을 기반으로 Agent가 이상 상황의 원인 후보와 조치 시나리오를 설명하는 Agent-assisted Simulation으로 제한한다.

장기적으로는 실제 현장 데이터, Digital Twin, Ontology, 예측 모델을 연결해 공정 이상 감지, 조치 시뮬레이션, Work Order 자동 제안으로 확장한다.

## 확장 기능 / 장기 방향

다음 기능은 장기 플랫폼화를 위해 고려하지만, v1에서는 범위를 조심한다.

1. **UDT**
   - 반복되는 설비/태그 구조를 타입화한다.
   - 현장별 커스터마이징을 제품 코어로 흡수하기 위한 기반이다.

2. **고급 Historian / 리포트**
   - 장기 데이터 분석, 설비별 리포트, 가동률/OEE 분석으로 확장한다.

3. **Schedule / Recipe / Batch 등 MES-lite 기능**
   - 완전한 MES를 v1 목표로 삼지 않는다.
   - 초기에는 작업 지시, 일정, 생산 흐름 일부만 다룬다.
   - v1 고객 노출 용어와 사용자 플로우(내부 `recipe/plan/task`는 유지, 고객 라벨·1~2단계 플로우는 분리)는 [mes-schedule](mes-schedule.md) SOT를 정본으로 따른다.

4. **Ontology / Graph 기반 원인 추론**
   - 설비, 태그, 알람, 로그, 문서 관계를 그래프로 연결한다.

5. **Predictive Model 기반 이상 감지**
   - 실제 현장 데이터가 쌓인 뒤 예측 모델로 이상 징후를 감지한다.

6. **Multi-Agent 운영 지원**
   - 장기적으로 운영, 유지보수, 분석, 작업지시 Agent를 분리한다.

7. **고급 Digital Twin**
   - 단순 3D 시각화를 넘어 실제 설비 데이터와 공정 관계를 반영한다.

8. **Robot / Humanoid Automation 연계**
   - 장기 비전에는 포함하되 v1에서는 제외한다.
   - 초기에는 사람이 승인하는 Work Order 흐름까지만 다룬다.

## MVP 제외 / 제한

v1에서는 다음을 의도적으로 제외하거나 제한한다.

- 기존 PLC 제어 로직을 대체하지 않는다.
- 실제 설비를 AI가 자동 제어하지 않는다.
- 공정을 완전히 예측한다고 주장하지 않는다.
- 사람 승인 없는 자동 조치를 제공하지 않는다.
- 복잡한 MES 전체를 구현하지 않는다.
- 범용 산업 전체를 한 번에 커버하려 하지 않는다.
- 복잡한 3D Digital Twin 완성도를 v1 성공 기준으로 삼지 않는다.
- Robot / Humanoid Automation은 장기 비전으로만 둔다.

기술 제약의 정본은 [agent-rules](agent-rules.md)와 [adr](../adr/index.md)를 따른다. 특히 No ORM, No plugin system, No config UI, 고정 expression 함수 제한은 v1 범위 통제 원칙이다.

## 디자인 방향

PureIron의 UI는 전통적인 낡은 HMI 느낌에 갇히지 않는다.

기본 방향은 Modern SaaS Dashboard와 산업 현장 가독성의 균형이다.

- SCADA 운영 도구처럼 보일 것 — 매일 쓰는 대시보드, 마케팅 페이지 아님.
- Mobile-first Alarm Handling을 핵심 경험으로 둘 것.
- 데이터 밀도, 명확한 상태 표현, 큰 버튼, 빠른 인지성을 우선할 것.
- 3D Digital Twin은 v1에서 고객 설득과 공정 이해를 돕는 데 집중할 것.
- AI-슬롭 안티패턴을 피할 것. 상세는 [UI_GUIDE.md](../../UI_GUIDE.md)를 따른다.

## 첫 계약 전략

PureIron의 첫 검증은 완성도 높은 기술 데모가 아니라 첫 유료 계약이다.

초기 전략은 완성형 SaaS가 아니라 현장 구축형 솔루션이다. 고객별 구축 과정에서 반복되는 화면, 태그 구조, 알람 조건, 공정 패턴, Work Order 흐름을 제품 코어로 흡수한다.

초기 영업 데모는 다음 가치를 짧고 명확하게 보여줘야 한다.

- 기존 설비를 유지하면서 시작할 수 있다.
- 웹/모바일로 설비 상태와 알람을 확인할 수 있다.
- Simulator와 3D Digital Twin으로 실제 공정처럼 보이는 데모를 볼 수 있다.
- Agent가 알람 원인 후보와 조치 시나리오를 설명한다.
- 외산 HMI/SCADA보다 가볍고 저렴하게 시작할 수 있다.

자세한 영업 전략, 가격, 채널, 세일즈 자료, PoC 패키지는 [first-contract](../gtm/first-contract.md) SOT에서 분리해 다룬다. 1차+추가 창업자 딥인터뷰로 첫 타깃 업종(금속·가공·프레스·사출 등 일반 중소 제조), 진입 채널, **read-only 중심 현장 PoC 범위**, 1순위 고객 페인, 첫 PoC PLC/프로토콜/네트워크 원칙, **온프레미스 우선 + 선택적 클라우드** 배포 방식, 단계형 PoC 기간(총 3주 이내), **가격 anchor**가 확정됐다. 중앙 관제/제어는 장기 방향일 뿐 v1/첫 계약에 약속하지 않는다. 가격 anchor는 시작 범위이며 실제 견적은 현장 조건별로 조정하고, 구체 고객사·현장 실측 구성 등 남은 항목은 그 문서에서 `needs-founder-input`으로 표기한다(상세는 [first-contract](../gtm/first-contract.md)).

## 성공 기준

v1의 성공 기준은 기능 개수가 아니라 고객 설득력이다.

핵심 성공 기준은 다음과 같다.

- 첫 유료 계약 가능성을 높이는가?
- 고객이 “현장 밖에서도 공장이 보인다”고 느끼는가?
- 모바일 알람 확인과 조치 흐름이 명확한가?
- Simulator + 3D Digital Twin + Agent-assisted Simulation이 데모에서 차별점으로 보이는가?
- 기존 설비를 불안정하게 만들지 않는가?
- 2인 팀이 만들고 유지할 수 있는 범위인가?
- 장기적으로 Agent, Digital Twin, Ontology, 예측 모델로 확장 가능한 데이터 구조를 남기는가?

## 성능 / 비기능 요구

기존 성능 목표는 유지한다.

- p95 응답 < 500ms
- ~1800 writes/sec 처리
- WebSocket 10개 동시 연결, 250ms 배치 전송

검증 기준은 [testing](testing.md)의 E2E AC/NF 표를 따른다.
