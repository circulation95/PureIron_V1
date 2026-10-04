---
id: sot-mes-schedule
title: MES/Schedule SOT — v1 용어와 작업 플로우 계약
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/mes/models.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/mes/router.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/agents/tools/schedule_tools.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ScheduleScreen.tsx
last_verified: 2026-07-12
---

# MES/Schedule SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

이 문서는 PureIron의 **MES-lite / Schedule 기능의 v1 고객 노출 용어와 사용자 플로우 계약의 정본**이다.

내부 도메인 모델(`recipe` / `plan` / `task` 3계층)은 유지하되, **고객이 보는 용어와 화면 플로우는 v1 도입 저항을 낮추는 방향으로 분리**한다는 결정을 담는다. 제품 상위 의도는 [product](product.md#확장-기능--장기-방향), API 계약은 [api-events](api-events.md), 화면 규칙은 [frontend](frontend.md)를 정본으로 따르고, 이 문서는 그 위에서 "MES/Schedule를 v1 고객에게 어떻게 부르고 어떻게 쓰게 하는가"만 다룬다.

> 배경: [#133](https://github.com/circulation95/PureIron_ignition/issues/133). #127(MES CRUD 인증/RBAC) 진행 중, 기존 PLC 로직이 이미 잘 도는 현장에 "레시피 생성 → 플랜 생성 → 작업 등록" 3단계 절차와 `Recipe`라는 용어를 전면 노출하면 "MES를 새로 운영하라"는 느낌을 줘 도입 저항이 커진다는 제품 우려가 제기됐다.

## 결정 상태 표기

[first-contract](../gtm/first-contract.md#결정-상태-표기)와 같은 표기를 쓴다.

| 표기 | 의미 |
|---|---|
| `확정` | 이 이슈에서 정해진 SOT 계약. 구현은 이 계약을 따른다 |
| `가설` | 검증 전 작업가설. 데모/현장 피드백으로 검증해야 함 |
| `needs-founder-input` | 창업자/대표 고객 확인 없이는 최종 확정하면 안 되는 항목(주로 브랜드 문구) |

## 1. 내부 모델 vs 고객 노출 라벨 분리

`확정` **내부 모델명과 고객 노출 라벨을 분리한다.** DB/API는 그대로 두고, 프론트엔드 표시 문자열만 바꾼다.

이유:
- 내부 3계층(`recipe`/`plan`/`task`)은 장기 에이전트 시나리오(아래 [4](#4-에이전트-연계-시나리오에서-3계층이-필요한-지점))에서 확장 가치가 있어 당장 대수술하지 않는다.
- DB/API migration을 함께 바꾸면 #127 같은 안전 경계 작업과 섞여 범위·리스크가 커진다.
- 라벨만 프론트에서 바꾸면 회귀 위험이 UI 문자열로 국한된다.

따라서 다음은 **v1에서 바꾸지 않는다**(내부 이름 유지):
- DB 테이블: `mes_recipes` / `mes_plans` / `mes_tasks`
- REST 프리픽스/경로: `/api/mes/recipes` · `/api/mes/plans` · `/api/mes/tasks`([api-events](api-events.md), `backend/src/pureiron/mes/router.py`)
- Pydantic 모델·에이전트 도구 인자: `Recipe*` / `Plan*` / `Task*`, `create_schedule(plan_id=...)`(`backend/src/pureiron/agents/tools/schedule_tools.py`)

## 2. v1 고객 노출 용어

`확정`. 내부 모델 → v1 고객 노출 라벨 매핑은 다음과 같다.

| 내부 모델 | v1 고객 노출 라벨 | 성격 |
|---|---|---|
| `Recipe` | **운전 조건** | 태그/셋포인트 묶음. 사용자는 "만든다"가 아니라 기존 조건을 **불러온다/복사한다**로 느끼게 한다 |
| `Plan` | (v1 UI에서 이름 노출 안 함) | 내부적으로만 생성하거나 고급 옵션으로 숨긴다 |
| `Task` | **작업** | 캘린더에 올라가는 실제 예약 작업. v1에서 사용자가 주로 다루는 1차 객체 |

`Recipe`의 v1 라벨 판단:

- 후보(이슈 #133): 작업 조건 세트 / 운전 조건 세트 / 공정 조건 템플릿 / 표준 조건 / 작업 템플릿.
- v1에서는 **"운전 조건"**으로 결정한다. 이유: (1) `Task`의 라벨 "작업"과 단어 충돌(작업 조건 세트/작업 템플릿)을 피한다. (2) "기존 공정의 운전 조건을 불러온다"는 v1 프레이밍에 맞는다. (3) "레시피"보다 업종 중립적이고 부담이 적다. (4) "공정 조건"은 공정 설계 자체를 새로 정의하는 뉘앙스가 있어 첫 PoC에서는 더 무겁다.
- 향후 특정 업종 현장 인터뷰에서 더 자연스러운 표현이 확인되면 표시 문자열은 조정할 수 있다. 단, v1 구현 이슈는 이 SOT 기준으로 `Recipe`를 "운전 조건"으로 노출한다.

`확정` `Recipe`를 영어 그대로 "Recipe"로 노출하지 않는다. 현재 화면(`Recipes`/`Plans` 버튼, "Manage recipes, production plans, and scheduled tasks" 카피)이 노출 그대로다 — v1 구현 시 위 매핑으로 교체한다(`frontend/src/components/ScheduleScreen.tsx`).

## 3. v1 사용자 플로우 (1~2단계)

`확정`(플로우 구조). 현재 플로우는 3단계 노출(레시피 관리 → 플랜 관리 → 작업 등록)이다. v1은 이를 **1차 1단계 + 선택적 고급 1단계**로 압축한다.

**v1 기본 플로우 (1단계): "작업 등록"**
- 진입점은 Schedule 화면의 "작업 등록"(현 `Add Task`) 하나다.
- 작업 등록 폼에서 사용자는 둘 중 하나로 조건을 채운다.
  - 기존 **운전 조건(세트)**(`Recipe`)을 선택한다, 또는
  - 최근 **작업**(`Task`)의 조건을 복사한다.
- `Plan`은 사용자가 명시적으로 만들지 않는다. 백엔드는 선택된 운전 조건에서 내부적으로 `plan`을 생성/재사용해 `task.plan_id`를 채운다(에이전트 `create_schedule`도 `plan_id`를 요구하므로, 이 자동 생성/선택 경로가 사람·에이전트 공통 진입점이 된다).

**v1 선택적 고급 플로우 (1단계): "운전 조건 관리"**
- 반복 사용할 운전 조건(세트)을 미리 정의/편집하는 화면은 **고급/설정 영역**으로 내린다. 1차 화면의 주요 버튼(`Recipes`/`Plans`)에서 빼고, 필요할 때만 진입한다.
- `Plan` 편집 UI는 v1 기본 노출에서 제외한다(내부 자동 생성 또는 고급 옵션).

즉 v1 노출 단계 수: **작업 등록(1) [+ 운전 조건 관리(선택)]**. "레시피 만들기 → 플랜 만들기 → 작업 등록" 3단계 학습을 요구하지 않는다.

> 이 절은 UX **계약**이다. 실제 프론트 구현(폼 재구성, 버튼 강등, 복사 기능)은 이 계약을 따르는 **후속 구현 이슈**에서 진행한다. 이슈 #133은 용어·플로우 결정과 SOT 반영까지를 범위로 한다.

## 4. 에이전트 연계 시나리오에서 3계층이 필요한 지점

`확정`(문서화). 내부 3계층은 아래 지점에서 실제로 쓰이므로 v1에서 제거하지 않는다.

- **지금(v1):** 에이전트 `create_schedule` 도구는 `plan_id`를 입력으로 받아 `task`를 만든다(`backend/src/pureiron/agents/tools/schedule_tools.py`, 프로덕션에서는 policy-first + approval 봉투). 따라서 에이전트가 작업을 예약하려면 `plan`이 존재해야 한다 → v1에서 `plan`은 최소/자동 생성으로 뒤에 남긴다. `task`는 사람과 에이전트가 공유하는 예약 실행 단위다.
- **장기:** 에이전트가 태그/공정 조건을 **추천·자동 구성**할 때 3계층이 각 층으로 자연스럽게 매핑된다.
  - `Recipe`(운전 조건) = 에이전트가 제안하는 태그/셋포인트 묶음(재사용 가능한 조건 세트).
  - `Plan` = 여러 조건을 순서/조합으로 엮은 다단계 공정(에이전트가 시퀀스를 구성).
  - `Task` = 그 조합의 예약 실행.
- 결론: 3계층은 **에이전트가 조건을 자동 생성/시퀀싱할 때 값을 한다.** 그 전까지는 내부 모델로 유지하고 고객 UI에는 점진적으로만 드러낸다([product](product.md#simulator--3d-digital-twin--ai-simulation-방향)의 "사람 판단을 돕는 역할" 제한과 일관).

## 5. v1 범위 밖 / 주의

- 이 문서는 **용어·플로우 계약**이다. DB/API 모델명 변경, migration, 프론트 대규모 재구성은 v1 이 이슈 범위가 아니다([1](#1-내부-모델-vs-고객-노출-라벨-분리)).
- 완전한 MES 운영(BOM, 실적 집계, 재고 연동 등)은 v1 목표가 아니다([product](product.md#mvp-제외--제한)).
- 에이전트가 사람 승인 없이 작업을 자동 실행하지 않는다. `create_schedule`는 프로덕션에서 approval 봉투를 반환한다([agent-rules](agent-rules.md#제품-판단-원칙)).
- 운전 조건 내려주기·작업 시작/정지는 **기존 PLC 주소 쓰기**로 한다. PLC 로직은 바꾸지 않으며, [ADR-013](../adr/index.md)(개정) 가드레일을 따른다: 프로젝트에 선언된 태그만(설정값은 범위, 명령은 PLC 허가 조건 필수), 고객이 켠 현장에서만, 사람 확인 또는 사람이 미리 승인한 예약 `작업`으로만 실행, 실행 전 검사·실행 후 readback·감사 로그, 자동 재시도 없음. 에이전트는 값을 제안만 한다.
