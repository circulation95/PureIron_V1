# UI 디자인 가이드

## 디자인 원칙

1. **SCADA 운영 도구처럼 보일 것** — 매일 쓰는 대시보드, 마케팅 페이지가 아니다.
2. **데이터 밀도 우선** — 한 화면에 가능한 많은 운영 정보. 빈 공간·과한 여백 지양.
3. **장식 최소화** — 그라데이션·블러·글로우는 거의 사용하지 않는다.
4. **다크 모드와 라이트 모드 모두 지원** — `theme` atom + `data-theme` on `<html>`.

## AI 슬롭 안티패턴 — 하지 마라

| 금지 사항 | 이유 |
|---|---|
| `backdrop-filter: blur()` | glass morphism은 AI 템플릿의 가장 흔한 징후. |
| gradient-text (배경 그라데이션 텍스트) | AI가 만든 SaaS 랜딩의 1번 특징. |
| "Powered by AI" 배지 | 기능이 아니라 장식. 사용자에게 가치 없음. |
| `box-shadow` 글로우 애니메이션 | 네온 글로우 = AI 슬롭. |
| 보라/인디고 브랜드 색상 (강조 외) | "AI = 보라색" 클리셰. 챗봇 UI 외에는 자제. |
| 모든 카드에 동일한 `rounded-2xl` | 균일한 둥근 모서리는 템플릿 느낌. 컴포넌트별로 의도적으로 변화. |
| 배경 gradient orb (`blur-3xl` 원형) | 모든 AI 랜딩 페이지에 있는 장식. |
| 과한 motion (fade + slide + scale 동시) | 어지러움. 한 번에 하나만. |

## 애니메이션과 알람 예외

공통 기준은 `frontend/src/styles/ds/motion.css`의 duration/easing token과 `frontend/src/lib/motion.ts`의 Motion용 mirror를 사용한다. 애플리케이션 루트의 `MotionConfig reducedMotion="user"`가 사용자의 `prefers-reduced-motion` 설정을 Motion 컴포넌트에도 적용한다.

허용 범위:

- 메뉴·popover·drawer의 짧은 fade 또는 slide 한 가지
- 실제 태그 값 변경에 따른 1회성 highlight 또는 단일 속성 보간
- 요청 처리 중임을 나타내는 loading spinner
- hover/focus의 짧은 색상·테두리 전환

금지 범위:

- 정상 연결, Live Feed, 가동 중 장비처럼 지속 상태를 반복 발표하는 pulse
- 페이지 진입 시 카드 stagger, hover scale, fade+slide+scale 조합
- neon/glow box-shadow와 데이터에 연결되지 않은 장식 애니메이션
- `transition-all`, 임의 duration/easing, 의미 없는 무한 반복

### Critical 미확인 알람 예외

운영 예외는 **Critical 우선순위이면서 아직 확인(acknowledge)되지 않은 알람** 하나뿐이다. 이 조건에서는 전역 Alarm Bell의 ring/bell cue가 운영자가 확인할 때까지 반복될 수 있다. 알람이 확인되거나 Critical 미확인 상태가 해소되면 반복 motion은 즉시 중단한다. Warning·Info와 일반 Active 상태는 색상, 아이콘, 카운트 등 정적 신호만 사용한다. `prefers-reduced-motion: reduce`에서는 Critical 예외도 정적 ring·색상·카운트로 대체한다.

## 색상 (예시 — 실제 토큰은 `frontend/src/styles/`에서 관리)

### 배경

| 용도 | 라이트 | 다크 |
|---|---|---|
| 페이지 | `#F0F2F5` | `#0a0a0a` |
| 카드 | `#FFFFFF` | `#141414` |

### 텍스트

| 용도 | 라이트 | 다크 |
|---|---|---|
| 주 텍스트 | `text-gray-900` | `text-white` |
| 본문 | `text-gray-700` | `text-neutral-300` |
| 보조 | `text-gray-500` | `text-neutral-400` |
| 비활성 | `text-gray-400` | `text-neutral-500` |

### 시맨틱 색상

| 용도 | 색상 |
|---|---|
| 정상/성공 | `#22c55e` (green-500) |
| 경고 (H/L) | `#f59e0b` (amber-500) |
| 심각 (HH/LL) | `#ef4444` (red-500) |
| 알람 비활성 | `#525252` (neutral-600) |
| 데이터 라인 | 카테고리별 고정 팔레트 (랜덤 색상 금지) |

## 컴포넌트 패턴

### 인증 화면 (`LoginPage`)
- 로그인은 별도 고정 dark 브랜드 화면이 아니라 전역 `theme` atom의 Light/Dark 정책을 따른다.
- `--auth-*` contextual token은 전역 surface/text/accent/status token을 의미별로 연결하며, 로그인 컴포넌트가 자체 raw palette를 만들지 않는다.
- 카드·입력·버튼의 radius, typography, focus-visible, motion은 공통 DS token 계약을 따른다. 오류는 색상만이 아니라 `role="alert"`와 invalid field 상태로 전달한다.
- 320px/393px에서도 폼과 긴 오류 문구가 viewport를 넘지 않아야 한다.

### 태그 값 카드
- 라벨 (path 또는 description) + 값 + 단위 + 상태 인디케이터
- 값 변경 시 짧은 하이라이트 (0.4s, 옅은 배경)

### 알람 행
- 색상은 시맨틱 색상 표 사용
- 시간 → 태그 → 메시지 → 우선순위 순서
- 활성 알람은 굵게, 확인된 알람은 옅게

### 트렌드 차트
- Recharts 사용
- 시간 축은 한국 시간대 표시 (`Asia/Seoul`)
- 다중 태그는 최대 6개 (그 이상은 가독성 저하)

### 챗봇 (`frontend/src/components/ChatBot.tsx`)
- SSE 스트리밍, 토큰 단위 업데이트
- 도구 호출은 `ToolCallIndicator`로 표시 (실행 중 / 완료)
- 승인 요청은 `ApprovalCard`로 (현재값 / 변경값 / 승인·거부 버튼)
- 도구명 한글 표시: `ChatBot.tsx`의 `TOOL_NAMES` 딕셔너리
- 코드블록은 `CodeBlock` (인라인 복사 버튼)
- `tag_reference` 이벤트 → `highlightedTagAtom` → 3D mesh 하이라이트

## 3D 트윈

- React Three Fiber, OrbitControls
- `scene.ts`의 `activeModelAtom`, `sceneConfigAtom`, `highlightedTagAtom`
- `NodeMapping`으로 mesh 이름 ↔ 태그 경로 바인딩
- 하이라이트는 색상 변경 + 외곽선 (애니메이션 1회만)

### 3D · P&ID operational canvas 경계

3D Twin과 P&ID처럼 설비 장면 자체를 탐색하는 viewport만 `operational-canvas` contextual surface를 쓴다. 일반 Dashboard/Monitoring의 KPI·표·상태는 계속 공통 `Card`·`DataTable`·`StatusBadge`를 사용하며 canvas 스타일을 가져오지 않는다.

- `.ds-operational-canvas`는 불투명한 theme surface, 표준 border, `--radius-3`만 소유한다. viewport 밖의 model selector·upload 등은 공통 `Button`·`Select`를 쓴다.
- `.ds-canvas-overlay`는 렌더링 장면 위에 떠야 하는 camera control·mesh label·readout에만 허용되는 translucency/blur 예외다. 일반 카드·표·dialog·상태 strip에는 사용하지 않는다.
- overlay는 가장 작은 `--shadow-1`만 허용하고 glow·큰 shadow·장식 animation을 금지한다. status strip과 loading/empty state는 불투명해야 한다.
- overlay의 작은 text/control은 실제 합성 배경에서 WCAG AA 4.5:1 이상, keyboard `focus-visible`은 `--border-focus`, coarse pointer control은 최소 44px을 지킨다.
- Light/Dark에서 canvas shell·overlay·status와 외부 control의 token binding을 각각 검증한다. Three.js material/scene 데이터 색상은 이 DOM surface 계약과 별도다.

### SCADA 운영 패널 (`scada-panel`)

`scada-panel`은 공정 SCADA 탭에서 운영자가 계속 읽는 trend·event log·device readout을 하나의 계기처럼 묶는 contextual surface다. 일반 KPI·설비 요약·line card는 계속 공통 `StatCard`·`Card`·`StatusBadge`를 사용하며 panel 스타일을 가져오지 않는다.

- `.ds-scada-panel`은 불투명한 theme surface, semantic border, `--radius-3`, 최소 `--shadow-1`만 소유한다. 3D 장면과 달리 뒤에 보여줄 renderer가 없으므로 translucency·blur·`canvas-overlay`를 사용하지 않는다.
- 상태는 화면 경계에서 `StatusBadge` tone으로 매핑하고 visible text+icon을 함께 제공한다. readout 값과 meter도 색상만으로 상태를 전달하지 않는다.
- chart grid·axis·tooltip처럼 theme에 종속되는 색상은 semantic token을 사용한다. 목표·허용오차·alarm 같은 공정 의미 색상도 `--status-*` token에 귀속한다.
- control은 공통 `Button`·`IconButton`을 사용하고 keyboard `focus-visible`, accessible name, coarse pointer 44px을 지킨다. simulation control을 실제 PLC command처럼 새로 연결하거나 표현하지 않는다.
- Light/Dark와 Desktop/Mobile에서 작은 text contrast 4.5:1, 긴 event wrap, document overflow, deterministic visual regression을 검증한다.

#### 8개 공정 surface inventory

| 공정 | Dashboard / Monitoring | Control / SCADA | 3D / P&ID |
| --- | --- | --- | --- |
| Inbound / Outbound | 공통 standard surface | 현재 mounted control 없음. `InboundOutboundScada.tsx`는 orphan이며 contextual 이관 전 재연결 금지 | `operational-canvas` + bounded `canvas-overlay` |
| Distribution | 공통 standard surface | Roaster Control: standard + opaque `scada-panel` | `operational-canvas` + bounded `canvas-overlay` |
| Extraction | 공통 standard surface | Extract Tanks: standard + opaque `scada-panel`; 기존 live tag/command 경로 유지 | `operational-canvas` + bounded `canvas-overlay` |
| Concentration | 공통 standard surface | Evaporator Control: standard + opaque `scada-panel` | `operational-canvas` + bounded `canvas-overlay` |
| Freeze Drying | 공통 standard surface | Lyophilizer Control: standard + opaque `scada-panel` | `operational-canvas` + bounded `canvas-overlay` |
| Packaging | 공통 standard surface | Packaging Line: `scada-panel` exemplar | `operational-canvas` + bounded `canvas-overlay` |
| Warehouse | 공통 standard surface | AS/RS View: standard + opaque `scada-panel` | `operational-canvas` + bounded `canvas-overlay` |
| Utility | 공통 standard surface | Water Distribution P&ID: `operational-canvas` exemplar; 도면 위 label/readout만 `canvas-overlay`, 하단 log는 `scada-panel` | 3D View는 `operational-canvas` + bounded `canvas-overlay` |

이 표는 실제 `ProcessLayout` route/tab에서 mounted된 surface만 분류한다. 새 전문 화면을 연결할 때는 ordinary surface와 renderer-backed canvas를 먼저 구분하고, orphan 파일을 그대로 mount하지 않는다.

## 탭 (`Tabs` / `TabList` / `Tab` / `TabPanel`)

콘텐츠 전환 탭은 `frontend/src/components/ui`의 공통 `Tabs` primitive를 쓴다. 화면마다 탭 스타일·상태를 다시 구현하지 않는다.

- **언제 `Tabs`인가** — 같은 영역의 콘텐츠를 peer view로 교체할 때(예: 시스템 기록의 운영 기록/서버 로그/서비스 로그, 공정 화면의 Overview/SCADA/3D). 화면(라우트)을 바꾸지 않고 콘텐츠만 바뀌면 `Tabs`.
- **`Tabs`가 아닌 것** — 같은 view의 표시 범위/모드를 바꾸는 compact single-select는 `SegmentedControl`, 사이드바처럼 계층을 이동하며 콘텐츠 영역 전체를 교체하는 navigation은 `SideNav` 몫이다.
- controlled `value`/`onValueChange` 계약을 쓰고, active 색상·focus는 raw class 대신 `--accent`/`--border-focus` token으로 표현한다.
- 접근성 계약은 primitive가 소유한다: `role="tablist"`/`"tab"`/`"tabpanel"`, `aria-selected`, `aria-controls`/`aria-labelledby`, roving tabIndex, Left/Right·Home/End 이동, disabled tab skip. overflow 시 tablist만 가로 스크롤하고 페이지 전체는 넘치지 않는다.

## 분절 컨트롤 (`SegmentedControl` / `Segment`)

한 view의 표시 범위/모드를 바꾸는 compact single-select는 공통 `SegmentedControl` primitive를 쓴다(예: 생산 일정의 Month/Week/Day). raw button/class 조합으로 다시 만들지 않는다.

- **언제 `SegmentedControl`인가** — 같은 콘텐츠를 유지한 채 그 범위/모드만 상호배타적으로 토글할 때. 콘텐츠 영역 자체를 peer view로 교체하면 `Tabs`, 계층을 이동하면 `SideNav`다.
- controlled `value`/`onValueChange`를 쓰고, 선택 상태는 indigo raw fill이 아니라 `--surface-card`/`--text-accent`/`--border-focus` token으로 표현한다.
- 접근성 계약은 primitive가 소유한다: `role="radiogroup"`+접근 가능한 이름, 각 `Segment`의 `role="radio"`/`aria-checked`/disabled, roving tabIndex, Left/Right·Up/Down·Home/End 이동, disabled option skip. 컨트롤은 자체 overflow를 흡수해 393px에서 페이지 전체 가로 overflow를 만들지 않는다.

## 사이드 내비게이션 (`SideNav` / `SideNavItem`)

사이드바처럼 계층을 이동하며 콘텐츠 영역 전체를 교체하는 navigation은 공통 `SideNav` primitive를 쓴다(예: 설정 화면의 Profile/System/Security/Notifications/Accounts/RAG Lab). raw button/class 조합으로 다시 만들지 않는다.

- **언제 `SideNav`인가** — 사이드바의 각 항목이 서로 다른 목적지이고 선택 시 콘텐츠 영역 전체가 바뀔 때. 같은 영역의 콘텐츠만 peer view로 교체하면 `Tabs`, 콘텐츠는 그대로 두고 범위/모드만 토글하면 `SegmentedControl`이다.
- controlled `value`/`onValueChange`를 쓰고, 활성 상태는 indigo raw fill과 회전 chevron이 아니라 `--accent-dim` 배경, `--accent` border, 대비가 확보된 `--text-1` label, `--border-focus` focus token으로 표현한다.
- 접근성 계약은 primitive가 소유한다: `nav` landmark + 접근 가능한 이름, 활성 항목 `aria-current="page"`, disabled 항목 계약, 공통 `focus-visible` ring. 각 항목은 실제 `<button>`이라 roving tabIndex가 아니라 Tab 키로 순차 이동한다. 항목은 full-width로 쌓여 긴 라벨은 ellipsis로 잘리고, 데스크톱/모바일 모두 페이지 가로 overflow를 만들지 않는다.

## 운영 상태 (`StatusBadge`)

장비 연결, 통신, 알람 등 운영 상태 라벨은 공통 `StatusBadge` primitive를 쓴다. 화면별 raw 색상·아이콘·pill class 조합으로 다시 만들지 않는다.

- tone은 `normal | info | warning | critical | offline | unknown`의 의미 계약을 사용한다. backend raw 값은 화면 경계에서 이 tone으로 명시적으로 매핑한다.
- 색상만으로 상태를 전달하지 않는다. 모든 badge는 항상 보이는 텍스트와 tone별 아이콘을 함께 제공한다.
- 정상 지속 상태에 pulse/glow를 쓰지 않고, light/dark 모두 `--status-*`, `--accent-*`, semantic surface token만 사용한다.
- 긴 라벨은 badge 안에서 ellipsis 처리하며 393px 페이지 가로 overflow를 만들지 않는다.

### Alarm severity·lifecycle 매핑

Alarm Summary/History의 desktop table·mobile card와 전역 Alarm drawer는 backend priority/state를 화면 경계에서 같은 tone으로 매핑한다. 단일 소스는 `frontend/src/lib/alarmHelpers.ts`(`priorityTone`·`severityTone`·`alarmStateTone`)다.

- priority/severity → tone: `Critical`·`High` → `critical`, `Medium` → `warning`, `Low` → `info`, `Diagnostic` → `unknown`.
- lifecycle state → tone: `Active/Unacked` → `critical`, `Active/Acked`·`Clear/Unacked` → `warning`, `Clear/Acked`·`Normal` → `normal`.
- severity(Critical/Warning/Info)와 lifecycle state(Active/Acknowledged/Clear)는 색상뿐 아니라 라벨과 tone별 아이콘으로 구분한다. 위 "Critical 미확인 알람 예외" 반복 motion 정책은 badge 통합과 독립적으로 유지한다.

## 공정 화면 (`ProcessLayout`)

- 라우트 상단에는 `PROCESS_META`의 이름·설명을 쓰는 공통 `PageHeader`를 둔다.
- 탭 컨트롤러: 공통 `Tabs` primitive 사용. Overview / 커스텀 SCADA / 3D View (선택적)
- 표시명·색상·아이콘·라우트의 **단일 진실 소스는 `PROCESS_META`** (`src/types/processes.ts`)
- 컴포넌트에 공정명·색상 하드코딩 금지

## 레이아웃

- 최대 너비: 컨텐츠 영역 `max-w-7xl` 또는 풀 너비 운영 화면
- 사이드바: 토글 가능, 접힘 상태는 컴포넌트 로컬 (영속 안 함)
- 그리드 간격: `gap-3` ~ `gap-4`, 섹션 간 `space-y-6` ~ `space-y-8`

## 타이포그래피

| 용도 | 스타일 |
|---|---|
| 페이지 제목 | `text-2xl font-semibold` |
| 섹션 제목 | `text-base font-semibold` |
| 카드 제목 | `text-sm font-medium text-neutral-400` |
| 본문 | `text-sm leading-relaxed` |
| 수치 (모니터링 값) | `font-mono text-xl tabular-nums` |

- 라우트 화면은 `PageHeader`/`PageTitle`을 사용하며 페이지당 `<h1>`은 하나만 둔다.
- 공정 탭·카드처럼 라우트 안에 포함되는 화면은 `SectionTitle`/`CardTitle`을 사용한다.
- 공통 구현은 `src/components/ui/Typography.tsx`, 시각 계약은
  `src/styles/ds/primitives.css`의 `ds-page-*`, `ds-section-title`,
  `ds-card-heading`, `ds-body`, `ds-caption`, `ds-numeric-value`가 소유한다.

## 애니메이션

허용:
- fade-in (0.3 ~ 0.4s)
- slide-up (0.4s)
- 값 변경 하이라이트 (0.4s)
- 로딩 스피너 (`animate-spin`)

금지:
- 무한 반복 애니메이션 (스피너 제외)
- 페이지 로드 시 staggered 등장 (사용자 시간 낭비)
- 마우스 hover 시 scale (운영 도구는 클릭 정확성이 우선)

## 아이콘

- Lucide 인라인 SVG, `strokeWidth={1.5}` 기본
- standalone 아이콘은 `Icon` primitive를 사용하고, `Button`·`IconButton`·`Tab`·`SideNav` 내부 아이콘은 공통 primitive CSS가 동일한 stroke를 강제한다.
- 아이콘 컨테이너(둥근 배경 박스)로 감싸지 않는다 (챗봇 아바타 같은 명확한 의도 외에는)
- 새 공정 아이콘은 `ProcessScreen.tsx`의 `ICON_MAP`에 추가
