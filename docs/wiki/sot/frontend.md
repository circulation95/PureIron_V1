---
id: sot-frontend
title: Frontend SOT — React·Jotai·SCADA UI·3D Twin
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/package.json
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/AGENTS.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/CLAUDE.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/atoms/tags.ts
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/ws/client.ts
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/types/processes.ts
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ui/index.ts
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ui/Tabs.tsx
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ui/SegmentedControl.tsx
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ui/SideNav.tsx
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/components/ui/StatusBadge.tsx
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/src/styles/ds/primitives.css
last_verified: 2026-07-23
---

# Frontend SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다. V1 새 코드의 작성 규약은 [coding-style](coding-style.md) 10절이 우선한다. **V1 `services/web` 현행은 16절**이다.

이 문서는 PureIron frontend의 **구조·상태·실시간·UI 작업 규칙**에 대한 정본이다. 상세 구현은 `frontend/src/**`가 정본이고, `frontend/AGENTS.md`와 `frontend/CLAUDE.md`는 도구별 operational cheat sheet다.

Frontend는 v1에서 “운영자가 매일 보는 경량 Web HMI/SCADA 화면”이어야 한다. SaaS 랜딩처럼 보이는 장식형 UI가 아니라, 태그·알람·트렌드·MES·3D Twin·AI Chat을 빠르게 읽고 조작할 수 있는 작업 도구가 목표다.

## 1. 문서 경계

| 문서 | 역할 |
|---|---|
| `docs/wiki/sot/frontend.md` | frontend 아키텍처와 변경 규칙의 정본 |
| `frontend/AGENTS.md` | frontend 작업 전 읽는 짧은 guardrail |
| `frontend/CLAUDE.md` | Claude Code용 atom/API/화면 cheat sheet |
| [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md) | 운영자 중심 UI 디자인 가이드(legacy-final) |
| `frontend/src/**` | 실제 구현과 타입의 최종 정본 |

이 문서는 코드 전체를 복붙하지 않는다. 신규 기능을 추가할 때 어떤 경계를 지켜야 하는지, 어떤 파일군을 같이 봐야 하는지만 고정한다.

## 2. 스택과 실행 명령

`frontend/package.json` 기준 스택:

- React 19 + TypeScript
- Vite 6
- Jotai
- React Router v7
- React Three Fiber + Drei + Three
- Recharts
- Lucide React
- Tailwind CSS v4 via `@tailwindcss/vite`
- `motion`, `react-grid-layout`, `react-markdown`

명령은 반드시 `frontend/`에서 실행한다.

```bash
npm run dev      # Vite dev server: http://localhost:3000
npm run lint     # tsc --noEmit
npm run build    # production build
```

Frontend 변경의 최소 검증은 `npm run lint`다. 라우팅, import, bundling, UI composition이 바뀌면 `npm run build`까지 실행한다.

## 3. 디렉터리 책임

| Path | Responsibility |
|---|---|
| `src/api/` | REST API client. API 함수는 auth atom을 직접 읽지 않고 `token: string | null`을 인자로 받는다. |
| `src/atoms/` | Jotai app state와 app-level hook. WS bridge도 여기서 연결한다. |
| `src/components/` | route screen과 재사용 UI. 큰 화면 변경은 회귀 위험이 높으므로 unrelated refactor 금지. |
| `src/components/process/` | 공정 화면 composition: Overview / SCADA / 3D View. |
| `src/components/scada/` | 공정별 SCADA visualization. |
| `src/components/twin/` | 3D Twin runtime, model loading, scene controls, tag mapping. |
| `src/components/dashboard/` | 대시보드 grid/widget 런타임. |
| `src/components/mes/` | MES recipe/plan/task/calendar UI. v1 고객 노출 라벨·플로우 계약은 [mes-schedule](mes-schedule.md). |
| `src/components/settings/` | 설정 화면 탭과 local UI state. |
| `src/config/` | frontend client-side config/registry. |
| `src/hooks/` | 재사용 React hook. |
| `src/lib/` | UI/helper utility. |
| `src/registry/` | tag registry bridge와 tag event helper. |
| `src/types/` | shared TypeScript type. |
| `src/ws/` | WebSocket client singleton. |

## 4. 상태 관리 원칙

Jotai state는 `src/atoms/`에 둔다. 컴포넌트 안에 임시 상태를 둘 수는 있지만, 앱 전체에서 공유되는 상태를 ad hoc global 변수나 별도 singleton으로 만들지 않는다.

현재 주요 atom 파일:

| File | Responsibility |
|---|---|
| `auth.ts` | 인증 상태, `localStorage('pureiron_auth')`, `useAuthFetch()` |
| `tags.ts` | WS 연결 상태와 live tag value map, `useTag()`/`useTags()`/`useWsSetup()` |
| `theme.ts` | theme, `localStorage('pureiron-theme')`, `<html data-theme>` |
| `language.ts` | app shell language, `localStorage('pureiron-language')`, server hydration from `GET /api/settings/me` |
| `scene.ts` | 3D Twin active model, scene config, highlighted tag |
| `trend.ts` | trend tag config |
| `dashboard.ts` | dashboard layout/widget state |
| `devices.ts` | device UI state |
| `alarmUi.ts` | alarm UI state |

규칙:

- `useWsSetup()`은 앱 루트에서 1회 호출한다.
- 컴포넌트는 live tag가 필요하면 `useTag()` 또는 `useTags()`를 사용한다.
- 컴포넌트에서 별도 WebSocket을 직접 열지 않는다.
- app-level 상태를 prop drilling으로 억지 전파하지 말고 atom/hook 경계를 사용한다.
- 언어 설정은 `languageAtom`과 `useLanguageSync()`가 담당하며, `GET/PATCH /api/settings/me`의 `language` 계약을 유지한다.
- 시스템 언어의 대상은 사용자가 수정하지 않는 제품 고정 UI 문자열이다. 언어 변경을 구독하는 화면은 새로고침 없이 반영해야 하며, navigation·Settings·공통 controls·Login·Dashboard 편집 UI·전역 Alarm drawer는 필수 동적 번역 범위다. 이후 운영 화면도 같은 계약으로 단계적으로 전환하며, 새 고정 문자열을 locale 하드코딩으로 추가하지 않는다.
- 사용자 저장 제목/캡션, seed 데이터, tag path, 설비명, 단위는 자동 번역하지 않는다. enum/quality/status의 표시 문자열은 locale별 mapper/translation key를 사용하고 raw 계약값은 유지한다.

## 5. WebSocket·실시간 태그 규칙

`src/ws/client.ts`의 `scadaWS`가 WebSocket singleton이다.

- URL은 현재 host 기준 `/ws`다.
- `ScadaWebSocket`은 reconnect backoff와 ref-counted subscription을 가진다.
- `subscribe(tags)`/`unsubscribe(tags)`는 중복 구독을 ref count로 처리한다.
- reconnect 후 현재 subscription set을 다시 전송한다.
- tag update는 `src/atoms/tags.ts`에서 Jotai `tagValuesAtom`에 반영하고, 같은 업데이트를 `registry.applyTagBatch()`로 전달한다.

금지:

- 컴포넌트에서 `new WebSocket(...)` 생성.
- `scadaWS.unsubscribe()`를 우회하는 cleanup.
- `registry.applyTagBatch()` 경로를 건너뛰는 tag update 반영.
- Device health를 tag로 취급하는 UI 계약. Device health는 backend `/api/devices` merged response가 정본이다.

## 6. API client 규칙

- API helper는 `src/api/`에 둔다.
- 함수는 `token: string | null`을 인자로 받는다. Jotai atom을 API layer에서 직접 읽지 않는다.
- 인증 header는 기존 `authHeaders(token)` 패턴을 따른다.
- 오류 처리는 기존 `handleResponse<T>()` 계열 패턴을 재사용한다. FastAPI `detail` 파싱 동작을 깨지 않는다.
- backend contract가 바뀌면 `docs/wiki/sot/api-events.md`와 관련 타입을 같이 확인한다.

## 7. TypeScript 규칙

절대 금지:

- `as any`
- `@ts-ignore`
- `@ts-expect-error`
- `tsconfig` strictness 약화
- lint/build 통과만을 위한 타입 무력화

대안:

- 명시적 interface/type 정의
- discriminated union
- `unknown` payload에 대한 type guard
- component boundary에서 props type 고정
- API/SSE payload는 좁혀서 처리

`npm run lint`는 ESLint가 아니라 `tsc --noEmit`이다. 따라서 타입 오류를 숨기는 방식은 허용하지 않는다.

## 8. 공정 화면 규칙

`src/types/processes.ts`의 `ProcessType`과 `PROCESS_META`가 frontend 공정 표시명·색상·아이콘·route의 단일 진실 소스다. Backend `ProcessType` enum과 동기화되어야 한다.

공정 화면 기본 구조:

```text
ProcessLayout
├── Overview        → ProcessScreen / ProcessTagPanel / ProcessSchedulePanel
├── Custom SCADA    → src/components/scada/*
└── 3D View         → ProcessTwinView, enable3D=true인 경우
```

새 공정 추가 시 최소 변경 범위:

1. `src/types/processes.ts`: `ProcessType`, `PROCESS_META`
2. `src/App.tsx`: route
3. `src/components/Layout.tsx`: sidebar item
4. `src/components/process/*Screen.tsx`: `ProcessLayout` 기반 화면
5. Backend process enum/contract 동기화

공정명, 색상, icon, route를 컴포넌트마다 하드코딩하지 않는다.

## 9. 3D Twin 규칙

3D Twin은 React Three Fiber 기반이다.

- scene 상태는 `scene.ts` atom을 통해 공유한다.
- `NodeMapping`은 mesh name과 tag path를 연결한다.
- `tag_reference` 이벤트는 `highlightedTagAtom`으로 들어가고, 3D mesh highlight로 연결된다.
- 3D View는 “보여주기용 장식”이 아니라 operator가 설비/태그 맥락을 이해하기 위한 화면이다.
- v1에서 실제 설비 자동 제어 또는 AI 기반 자동 제어를 3D UI에 추가하지 않는다. 위험 액션은 backend 승인 흐름과 UI 승인 UX를 유지한다.

## 10. ChatBot SSE 규칙

주 구현은 `src/components/ChatBot.tsx`다. 샘플/복사본 파일은 명시적으로 그 파일을 수정하는 작업이 아니면 reference로만 본다.

보존해야 하는 SSE event type:

- `token`
- `tool_call`
- `tool_result`
- `approval_request`
- `tag_reference`
- `error`
- `done`

규칙:

- 위험 액션은 `ApprovalCard` 승인 UX를 거친다.
- UI에 direct-execute path를 추가하지 않는다.
- 도구명 한글 표시와 code block copy UX는 유지한다.
- `tag_reference`는 3D highlight와 연결된다.

## 11. Operator UI 규칙

PureIron frontend는 운영자 중심 SCADA UI다. [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md)가 상세 디자인 가이드다.

우선순위:

1. 한 화면에서 핵심 운영 정보를 빠르게 읽을 수 있는 데이터 밀도
2. 상태·알람·품질·시간의 시맨틱 표현
3. 예측 가능한 조작: button / toggle / numeric input / tab
4. desktop과 mobile에서 텍스트가 깨지지 않는 레이아웃
5. 과한 장식보다 장시간 사용 가능한 절제된 UI

금지:

- marketing landing page 스타일
- glass morphism, blur orb, gradient text, neon glow
- “Powered by AI” 같은 가치 없는 장식 배지
- 무의미한 hover scale과 과한 page-load animation
- nested card 남발
- 랜덤 chart color

허용되는 animation은 값 변경 highlight, 짧은 fade/slide, loading spinner 정도다.

## 12. 변경·검증 원칙

Frontend 작업은 화면이 크고 회귀 위험이 높다. 다음 원칙을 지킨다.

- 요청 범위 밖 unrelated refactor 금지.
- 기존 route/sidebar entry 삭제는 명시 요청 없이는 금지.
- generated/dependency file은 dependency update 작업이 아니면 수정 금지.
- backend contract가 바뀌면 frontend type/API client와 `api-events.md`를 함께 본다.
- UI 변경은 가능하면 작은 화면/컴포넌트 단위로 나누고, 각 단계마다 `npm run lint` 또는 `npm run build`로 확인한다.

검증 기준:

| 변경 유형 | 최소 검증 |
|---|---|
| Type/API/hook 변경 | `npm run lint` |
| Route/import/component composition 변경 | `npm run lint && npm run build` |
| Backend contract 동반 변경 | frontend 검증 + 관련 backend test |
| SOT 문서 변경 | repo root에서 `python scripts/wiki.py build && python scripts/wiki.py check` |

## 13. Design system primitive 계약

업무 화면의 공통 control과 surface는 `frontend/src/components/ui/index.ts`의 public API를 사용한다. 시각 계약은 `frontend/src/styles/ds/primitives.css`가 소유하며, 화면이 같은 button/input/card/table/state class bundle을 다시 정의하지 않는다.

| Primitive | 용도와 불변 계약 |
|---|---|
| `Button`, `IconButton` | `primary/secondary/danger/ghost`, `sm/md`, loading·disabled, 공통 `focus-visible`, coarse pointer 44px touch target. Icon-only control은 접근 가능한 `label` 필수. |
| `Field`, `Input`, `Select`, `Textarea`, `SearchInput` | label/control/message id와 `aria-invalid`를 연결하고 semantic surface/border/focus token을 사용한다. |
| `Card`, `SectionCard`, `StatCard` | 업무 surface·section heading·stat tone을 통일한다. nested card는 피한다. |
| `DataTable`, `SortableHeader`, `Pagination` | table semantics, `aria-sort`, loading/error/empty row, pagination status를 통일한다. 복잡한 계층형 table도 최소한 `ds-table` visual contract를 사용한다. |
| `EmptyState`, `LoadingState`, `ErrorState`, `Alert` | 정상 empty와 장애 error를 구분하고 live region 및 recovery action을 제공한다. |
| `Tabs`, `TabList`, `Tab`, `TabPanel` | 같은 영역의 peer view 콘텐츠 전환을 통일한다. controlled `value`/`onValueChange`, `role="tablist"`/`"tab"`/`"tabpanel"`, `aria-selected`, `aria-controls`/`aria-labelledby`, roving tabIndex, Left/Right·Home/End 이동, disabled tab skip, overflow 시 tablist만 가로 스크롤. active 색상·focus는 `--accent`/`--border-focus` token으로만 표현한다. |
| `SegmentedControl`, `Segment` | 한 view의 표시 범위/모드를 바꾸는 compact single-select를 통일한다(예: 생산 일정 Month/Week/Day). controlled `value`/`onValueChange`, `role="radiogroup"`+접근 가능한 이름, `role="radio"`/`aria-checked`/disabled, roving tabIndex, Left/Right·Up/Down·Home/End 이동, disabled option skip, 컨트롤 자체 overflow 흡수로 페이지 가로 overflow 방지. 선택 상태는 raw fill이 아니라 `--surface-card`/`--text-accent`/`--border-focus` token으로 표현한다. |
| `SideNav`, `SideNavItem` | 사이드바 계층 이동을 통일한다(예: 설정 화면 Profile/System/Security/Notifications/Accounts/RAG Lab). controlled `value`/`onValueChange`, `nav` landmark + 접근 가능한 이름, 활성 항목 `aria-current="page"`, disabled 항목 계약, 공통 `focus-visible`. 각 항목은 실제 `<button>`이라 roving tabIndex가 아니라 Tab 키로 순차 이동한다. 활성 상태는 raw indigo fill이 아니라 `--accent-dim` 배경, `--accent` border, AA 대비를 유지하는 `--text-1` label, `--border-focus` focus token으로 표현하고, full-width 항목은 데스크톱/모바일 모두 페이지 가로 overflow를 만들지 않는다. |
| `StatusBadge` | 장비·통신·알람의 운영 상태를 `normal/info/warning/critical/offline/unknown` tone으로 통일한다. backend raw status는 화면 경계에서 tone으로 매핑하고, 모든 상태는 색상뿐 아니라 보이는 text+tone별 icon을 함께 제공한다. 지속 정상 상태에 pulse/glow를 쓰지 않으며 semantic status/surface token만 사용하고 393px 페이지 가로 overflow를 만들지 않는다. Alarm severity(priority)·lifecycle state 매핑 단일 소스는 `frontend/src/lib/alarmHelpers.ts`이고, Alarm Summary/History와 전역 Alarm drawer가 이를 공유한다(매핑 표는 [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md)). |

**Tabs vs SegmentedControl vs SideNav 경계** — `Tabs`는 한 영역의 콘텐츠를 peer view로 교체할 때(예: 시스템 기록 3분류, 공정 Overview/SCADA/3D). 같은 콘텐츠의 표시 범위/모드만 상호배타적으로 토글하면 `SegmentedControl`(예: 생산 일정 Month/Week/Day), 사이드바 계층을 이동하며 콘텐츠 영역 전체를 교체하면 `SideNav`(예: 설정 화면 하위 카테고리)가 담당한다. 화면을 바꾸지 않고 콘텐츠 자체를 바꾸면 `Tabs`, 콘텐츠는 그대로 두고 범위/모드만 바꾸면 `SegmentedControl`, 목적지(콘텐츠 영역 전체)를 바꾸는 사이드 navigation이면 `SideNav`가 정답이다.

추가 규칙:

- primitive CSS는 `styles/ds/tokens.css` 계열 semantic token만 참조하고 raw hex/palette를 추가하지 않는다.
- `frontend/src/index.css`에서 `styles/ds/primitives.css`를 한 번만 import한다.
- Dashboard 전용 `ds-btn`/`ds-icon-btn` 구현을 별도로 유지하지 않는다. 동일 class contract는 primitives가 소유한다.
- 대표 관리 화면 migration과 primitive 변경은 light/dark, keyboard focus, disabled/error/empty 상태를 E2E 또는 component contract로 검증한다.

## 15. Overlay 계약

제품 화면의 modal surface는 `frontend/src/components/ui/index.ts`가 공개하는 `Dialog`, `Drawer`, `ConfirmDialog`를 사용한다. 화면별 `fixed inset-0` backdrop이나 임의 z-index를 새로 만들지 않는다.

- `Dialog`와 `Drawer`는 portal, overlay stack, Escape, focus trap/restore, scroll lock, background `inert`를 공통 소유한다.
- `sm/md/lg/wide` 크기는 primitive의 size prop으로 선택하고 화면별 width/backdrop/radius/shadow 계약을 만들지 않는다.
- 파괴적 작업은 안전한 취소 버튼에 initial focus를 두는 `ConfirmDialog` 또는 `useConfirm()`을 사용한다. 신규 제품 flow에 native `window.alert()`/`window.confirm()`을 추가하지 않는다.
- `dismissible=false`인 busy overlay는 Escape, backdrop, header close button으로 닫히지 않아야 한다.
- migration은 대표 Desktop/Mobile keyboard E2E로 focus trap/restore, Escape, background `inert`, 중첩 overlay 순서를 검증한다.
- 기존 화면 migration은 회귀 위험을 줄이기 위해 Device/Dashboard/Settings → Tag/UDT → Schedule/MES → Trend/ChatBot 순서의 작은 PR로 진행한다.

## 16. V1 web (M3b)

> 이 절은 **PureIron_V1 현행**이다(위 1~15절은 legacy 기준). 근거: plan M3b D10·D13·D14·§2.5, 규약은 [coding-style](coding-style.md) 10절.

**스택**: Vite + React 19 + TypeScript strict, `openapi-fetch`(생성 타입), `react-router`(라우팅·로그인 보호), `uplot`(트렌드 canvas — 1h 원시 3,600점 × 여러 태그), `@playwright/test`(devDep). 상태 라이브러리·UI 키트는 없다(React state + WS 클라이언트 모듈, 스타일은 `src/styles.css`). 편집 화면·쓰기 UI는 없다(No config UI, PLC 쓰기는 M3c).

| 경로 | 책임 |
|---|---|
| `src/App.tsx` | 경로 표. `/login` 밖은 모두 `Layout` 아래 |
| `src/app/Layout.tsx` | `GET /auth/me`로 세션 확인, 상단 내비·사용자/역할·로그아웃·미확인 알람 배지·실시간 연결 상태, WS 연결 하나와 알람 목록 소유 |
| `src/app/session.ts` · `alarms.ts` | 세션 context(`useSession`), 알람 목록 갱신 규칙(서버와 같은 집합·순서) |
| `src/api/client.ts` | `openapi-fetch` 클라이언트(`credentials: "include"`), 401 → 로그인 이벤트, 역할 비교·실패 문구, 생성 타입 별칭 |
| `src/api/schema.d.ts` | 생성물(`npm run gen:api`, contracts-drift가 검사). REST·WS 메시지 타입의 정본 |
| `src/ws/client.ts` | `RealtimeClient` |
| `src/pages/` | `Login` · `Overview`(태그) · `Alarms`(활성·ACK·저널) · `Trend` · `Devices` · `Audit` |
| `e2e/` · `playwright.config.ts` | compose 스택 위 chromium 스모크 |

**상태 규칙**
- 비동기 화면 상태는 `Load<T>` = `{kind:"loading"} | {kind:"ok",value} | {kind:"error",message}`. 응답 타입을 손으로 다시 선언하지 않는다.
- effect는 정리 함수(AbortController·WS 해제·ResizeObserver/uPlot 해제)를 돌려준다. 다시 불러올 때의 loading 전환은 effect가 아니라 이벤트 핸들러에서 한다.
- 실패 문구는 HTTP 상태로 고른 고정 문구다(서버 문구를 그대로 보여주지 않음). 로그인 실패는 "아이디 또는 비밀번호가 올바르지 않습니다."
- 401이 오면(로그인 요청 제외) `/login?next=<원래 경로>`로 보내고, 로그인 뒤 그 경로로 돌아간다(같은 사이트 경로만).
- 역할별 표시: ACK·전체 ACK는 운전원 이상, 감사 로그는 엔지니어 이상(아래는 권한 없음 안내). 서버가 같은 규칙으로 403을 낸다.

**WS** (`/api/v1/ws`, 쿠키 인증)
- 로그인한 레이아웃이 연결 하나를 만들고 화면이 `on(type, handler)`·`subscribe(tagIds)`(참조 계수)로 나눠 쓴다. 해제 함수를 effect 정리에서 부른다.
- 끊기면 0.5s부터 두 배씩 최대 10s 간격으로 재연결, `hello`를 받으면 구독 전체를 다시 보낸다. `ping`에는 `pong`. 서버는 20s마다 ping을 보내므로 50s 동안 아무 메시지도 없으면 죽은 연결로 보고 소켓을 버린 뒤 같은 backoff로 재연결한다.
- 재연결(`hello`)마다 알람·장비 목록은 REST로 다시 받아 끊긴 동안 놓친 이벤트를 메운다(태그는 재구독 때 서버가 현재값을 보냄).
- 알람 목록: 스냅샷 요청(제한 시간 10s) 중 도착한 `alarm_event`는 버퍼에 모았다가 스냅샷 적용 뒤 도착 순서대로 재생한다. `changed_at`은 이 합치기에서만 비교한다(같은 알람의 스냅샷이 더 새로우면 그 이벤트는 건너뜀). 그 뒤 실시간 이벤트는 비교 없이 순서대로 적용한다. ACK·ack-all 응답은 목록에 이미 있는 알람만 갱신한다. 재조회가 실패해도 기존 목록·미확인 배지를 유지하고 버퍼를 그 위에 재생한다.
- close 4401 → 로그인으로, 4403(Origin ≠ Host) → 재연결하지 않음. 그래서 nginx는 `Host $http_host`(포트 포함), vite proxy는 `changeOrigin`을 켜지 않는다.

**명령**

```bash
cd services/web && npm ci && npm run typecheck && npm run lint && npm run build
npm run dev                      # http://localhost:3000 → core 127.0.0.1:18000 (PUREIRON_CORE_URL)
bash scripts/g1.sh web-e2e       # 이미지 빌드 → pureiron-it compose(시뮬레이터 100태그 + 알람 1개, CLI 계정) → Playwright
```

e2e 결과·trace·화면 스크린샷은 `artifacts/web-e2e/`(`test-results/`·`report/`·`screens/`·`compose.log`). `PUREIRON_IT_KEEP=1`이면 스택을 내리지 않는다. 포트·프로젝트 이름은 core 통합 테스트와 같은 환경변수(`PUREIRON_IT_PROJECT`·`PUREIRON_DEV_*_PORT`·`PUREIRON_IT_WEB_PORT`)로 바꾼다.
