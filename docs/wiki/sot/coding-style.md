---
id: sot-coding-style
title: Coding Style SOT — V1 코드 구조·추상화·이름·주석·테스트 규약
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - pyproject.toml
  - services/core/pyproject.toml
  - services/core/.importlinter
  - services/core/tests/test_importlinter_negative.py
  - scripts/g1.sh
  - services/core/src/pureiron_core/runtime.py
  - services/core/src/pureiron_core/app.py
  - services/core/src/pureiron_core/historian/ports.py
  - services/core/src/pureiron_core/historian/service.py
  - services/core/src/pureiron_core/historian/writer.py
  - services/core/src/pureiron_core/tags/store.py
  - services/core/src/pureiron_core/api/health.py
  - services/core/src/pureiron_core/platform/migrate.py
  - services/core/src/pureiron_core/platform/db.py
  - services/core/src/pureiron_core/settings.py
  - services/core/src/pureiron_core/cli.py
  - services/core/tests/test_historian_writer.py
  - services/web/tsconfig.json
  - services/web/eslint.config.js
  - services/web/src/App.tsx
  - services/web/src/api/client.ts
last_verified: 2026-10-11
---

# Coding Style SOT

이 문서는 PureIron_V1 새 코드(`services/`·`packages/`·`deploy/`·`tests/e2e/`)를 **어떻게 쓰는가**의 정본이다.
1~9절은 M1~M3a 코드에 이미 반복되는 패턴을 글로 옮긴 것이고(어긋나는 기존 코드는 12절 백로그), 10절
프론트엔드는 코드가 아직 작아 **처방**이다. 무엇을 만드는가(설계 불변 규칙)는 [agent-rules](agent-rules.md)와
[ADR](../adr/index.md)가 정본이다. legacy 기준인 [frontend](frontend.md)·[testing](testing.md)의 코드 규칙과
이 문서가 다르면 V1 코드에는 이 문서가 우선한다.

## 1. 우선순위

판단이 갈리면 이 순서로 고른다.

1. **정확성** — 게이트(2절)와 테스트가 먼저다.
2. **읽는 사람이 한 번에 이해하는가** — 2인 팀이 몇 달 뒤 다시 읽는다. 영리한 코드보다 뻔한 코드가 낫다.
3. **가장 짧은 올바른 코드** — 지금 필요 없는 추상화·옵션·확장점은 만들지 않는다(4절). 줄 수를 줄이려고
   읽기 어렵게 만드는 것은 짧은 것이 아니다.
4. **주변 코드와 같은 모양** — 같은 일을 하는 코드가 이미 있으면 그 이름·구조·관용구를 따른다.

## 2. 기계 게이트 (어기면 CI 실패)

| 대상 | 게이트 | 설정 위치 |
|---|---|---|
| Python 문법·스타일 | ruff `E F W I B UP SIM RUF N ASYNC PT C4 PIE RET PL`, 줄 100자 | 루트 `pyproject.toml` `[tool.ruff]` |
| Python 타입 | pyright strict | 서비스·패키지별 `pyproject.toml` `[tool.pyright]`; `deploy/`·`packages/contracts/`·`tests/e2e/`는 `pyrightconfig.json` |
| core 모듈 경계 | import-linter 계약 (a)(b)(c) — 3절 | `services/core/.importlinter` |
| TypeScript | tsc `strict` + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` 등 | `services/web/tsconfig.json` |
| TypeScript lint | typescript-eslint `strictTypeChecked` + `stylisticTypeChecked` + react-hooks | `services/web/eslint.config.js` |
| 억제·우회 | 0건. 정본 패턴은 `scripts/g1.sh` no-suppressions job(`type: ignore`, `noqa`, `pyright: ignore/basic/standard/reportGeneralTypeIssues`, `fmt: off/skip`, `pragma: no cover`, `eslint-disable`, `as any`, `@ts-ignore`·`@ts-expect-error`·`@ts-nocheck`, `app.state`) | `scripts/g1.sh` |
| 생성 코드 | `_gen/`·`schema.d.ts`는 lint 제외, 대신 drift 0 | ADR-028 |

- ruff는 `PLR0913`(인자 수)·`PLR2004`(매직 넘버)만 전역으로 끈다. 인자가 많으면 선택 인자를 키워드 전용(`*,`)
  으로 둔다(6절). 매직 넘버는 끈 대신 상수로 이름을 붙이는 것을 기본으로 한다(5절).
- 규칙 예외는 줄 단위 억제가 아니라 `pyproject.toml`에서 **범위를 좁혀** 둔다: 파일 단위 `per-file-ignores`
  (예: pymodbus 콜백 시그니처) 또는 이름 단위 `pep8-naming.extend-ignore-names`(예: proto rpc 이름을 따르는
  gRPC servicer 메서드 `Session`). 새 예외에는 이유 주석을 붙이고 리뷰에서 확인받는다.
- 게이트가 잡는 것은 리뷰에서 다시 지적하지 않는다. 리뷰는 3~10절처럼 게이트가 못 잡는 것을 본다.

## 3. core 모듈 구조

```
services/core/src/pureiron_core/
  runtime.py      유일한 조립 지점: SiteRuntime.build(settings) — 모든 객체를 만들고 생성자로 주입
  app.py          create_app(rt): HTTP 노출만. 라우터 팩토리에 rt의 부품을 넘긴다
  cli.py          진입점(pureiron-core). runtime·app을 import하는 곳
  api/            HTTP 계층: build_<x>_router(deps) + pydantic 응답 모델
  <domain>/       acquisition · historian · project · tags (M3b: alarms · auth · audit · realtime · devices)
    ports.py      다른 모듈이 기대도 되는 유일한 표면: 상태 dataclass · Protocol · 공개 예외
    service.py    수명주기(start/stop)와 status 집계 — 필요할 때만
    <역할>.py      구현: writer · recorder · reader · store · ingest …
  platform/       도메인 무관 기반: db · bus · migrate · sqltext
  settings.py     환경변수 → frozen dataclass
```

- 의존 방향은 `api > domain > platform`(계약 (b)). 도메인끼리는 **상대 모듈의 `ports`만** import한다(계약 (c)).
  `runtime`·`app`을 import하는 것은 진입점뿐이다(`app`이 `runtime`을 쓰는 것은 제외) — 도메인·api·platform은 import하지 못한다(계약 (a)).
  **새 도메인 모듈을 만들면 `.importlinter`의 (a)(b)(c) 세 곳에 모두 추가**한다 — 빠지면
  `tests/test_importlinter_negative.py`가 실패한다.
- 값 객체와 모듈 경계를 넘는 상태 스냅샷은 `@dataclass(frozen=True, slots=True)`다. 한 소유자 안의 누적 통계·
  세션 상태는 `@dataclass(slots=True)` 가변을 허용하고(예: `SiteRuntime`, `RecorderStats`, collector
  `DeviceHealth`), 밖으로 낼 때는 frozen 스냅샷으로 복사한다(예: `Historian.status()` → `HistorianStatus`).
- HTTP 경계 모델만 pydantic `BaseModel`이다. 도메인 상태를 API 모델로 옮기는 변환은 `api/`에서 한다
  (예: `HistorianStatus` → `api/health.py` `Historian`).
- 라우터는 전역을 읽지 않고 `build_<x>_router(...)`가 받은 의존성(Protocol 타입)만 쓴다. `app.state`·
  모듈 전역 싱글톤·service locator 금지.
- 다른 서비스(collector·updater·control-api)와 `packages/*`도 같은 원칙이다: 진입점(`main.py`·`cli.py`)에서
  조립하고, 나머지는 주입받는다.

## 4. 추상화 기준

**Protocol(인터페이스)을 만드는 경우는 넷이다.**

1. 다른 도메인 모듈이 기대는 표면 → `ports.py` (예: `HistorianStatusSource`).
2. I/O 경계를 테스트에서 가짜로 바꿔 끼워야 할 때 (예: `RowStore` ← 실제 `PgRowStore`, 테스트 `FakeStore`).
3. 실제 구현이 이미 둘 이상일 때.
4. 타입이 없는 외부 라이브러리 표면을 strict 타입으로 좁힐 때 — 모듈 내부 `_` Protocol
   (예: `platform/migrate.py` `_YamlLoader`, `pureiron_bundle/manifest.py` `_Validator`).

그 밖에는 구체 클래스나 함수로 쓴다. "나중에 바뀔지 몰라서"는 이유가 되지 않는다.

| 쓴다 | 쓰지 않는다 (이유) |
|---|---|
| 생성자 주입 + `typing.Protocol`(구조적 타입) | ABC 상속 트리 — 구현이 하나면 간접층만 늘어난다 |
| 값 객체는 frozen dataclass, 가변 상태는 소유자 하나(3절) | 여러 객체가 같이 고치는 가변 공유 상태 |
| `Enum`/`StrEnum`/`IntEnum`으로 상태·품질 표현 | 문자열·정수 리터럴 비교 |
| 작은 함수 + 명시적 분기 | 레지스트리·플러그인·동적 import — 불변 규칙 "No plugin system" |
| 예외 계층(모듈 기준 1단계) | 제네릭 Repository·추상 Factory·메타클래스 |
| 표준 라이브러리 → 이미 쓰는 의존성 → 새 의존성 순 | 한두 함수 때문에 새 패키지 추가 |

- 상속은 예외 계층(`MigrationError` ← `LockTimeoutError` …)과 Protocol 합성(`AcquisitionStatus(CollectorSessionStatus, Protocol)`)에만 쓴다.
- 래퍼·헬퍼는 두 번째 사용처가 생길 때 추출한다. 한 곳에서만 쓰는 헬퍼는 그 자리에 둔다(작은 `_` 함수 허용).

## 5. 이름

| 대상 | 규칙 | 예 |
|---|---|---|
| 모듈 | 역할 명사, snake_case | `writer.py`, `recorder.py`, `store.py`, `ports.py` |
| 클래스 | 역할 명사 | `HistoryWriter`, `Recorder`, `TagStore`, `SiteRuntime` |
| asyncpg 구현 | 같은 Protocol의 DB 구현이면 `Pg` 접두 | `PgRowStore`, `PgProjectStateStore`, `PgDefinitionsSource` |
| Protocol | 제공하는 것 기준. 구체 클래스와 이름이 겹치면 `…Port` | `RowStore`, `HistorianStatusSource`, `DatabaseProbe`, `TagStorePort` |
| 예외 | `…Error`, 모듈 기반 클래스에서 파생 | `MigrationError`, `EditLockConflictError` |
| 상수 | `UPPER_SNAKE`, 기본값은 `DEFAULT_` 접두 | `DEFAULT_MAX_ROWS`, `QUERY_TIMEOUT_S` |
| SQL 문자열 | 모듈 상단 `<동작>_SQL` 상수 | `INSERT_SQL` |
| 시간 값 | 필드·인자·상수에 단위 접미사 `_s` `_ms` `_ns` `_us` | `write_lag_s`, `ts_ns`, `POLL_MS` |
| 크기 값 | 필드·인자·상수에 단위 접미사 `_bytes`(개수면 `_rows` 등) | `size_bytes`(bundle-metrics), `flush_max_rows` |
| 라우터 팩토리 | `build_<x>_router` | `build_health_router` |
| 모듈 내부 | `_` 접두 | `_schema_check`, `_int` |
| 환경변수 | 사이트 공용 값은 `PUREIRON_<이름>`, 새 서비스 전용 튜닝 값은 `PUREIRON_<서비스>_<이름>`, 비밀값은 `…_FILE` | `PUREIRON_SITE_ID`, `PUREIRON_COLLECTOR_HEARTBEAT_S`, `PUREIRON_DB_PASSWORD_FILE` |

- 단위 접미사의 예외: 함수 안 짧은 지역 변수(`delay`, `deadline`), 외부 API 시그니처를 따르는 이름
  (`subprocess`·`asyncio`의 `timeout`), `timedelta`처럼 타입이 단위를 담는 값(`RAW_MAX_SPAN`). `Pg` 접두의 예외: Protocol 짝이 없는 기반 클래스(`Database`, `SchemaGuard`).
- 축약은 업계 표준(`db`, `ts`, `rev`, `gen`, `dsn`)만 쓴다. 한 글자 이름은 컴프리헨션·짧은 람다 안에서만.
- bool은 상태를 말하는 이름(`db_ok`, `connected`)으로 쓴다. 상태·API로 노출하는 누적값은 `_total` 접미사
  (`dropped_total`, `devices_total`)를 쓴다. 모듈 내부 통계 필드(`RecorderStats.raw`)와 이미 정해진 계약 필드
  (`collector.proto` `timeout_count`)는 예외 — 계약 필드 이름은 바꾸지 않는다.

## 6. 함수와 파일 크기

- 함수는 한 가지 일을 한다. 분기·문장 수 한도는 ruff `PL`이 강제한다. 리뷰 기준은 **화면 한 장(약 50줄)**
  을 넘으면 나눌 수 있는지 본다.
- 파일은 **약 300줄**을 넘으면 역할별 분리를 검토한다. 줄 수 자체보다 "이 파일의 역할을 한 문장으로 말할 수
  있는가"가 기준이다 — 모듈 docstring 첫 줄이 그 문장이다. 300줄을 넘는 기존 파일(예: updater `phases.py`,
  e2e `run.py`)은 그 파일을 크게 고칠 때 분리를 검토한다.
- 선택 인자는 키워드 전용으로 둔다: `def __init__(self, recorder, writer, *, stop_timeout_s=DEFAULT_STOP_TIMEOUT_S)`.
- 시간·난수처럼 테스트에서 통제해야 하는 것은 인자로 주입한다: `clock: Callable[[], float] = time.monotonic`.
- `__init__.py`를 뺀 새 Python 파일은 `from __future__ import annotations`로 시작한다(빠진 기존 파일은 손댈 때
  맞춘다). 타입은 `X | None`, `type Alias = …`(3.12 문법)을 쓴다.

## 7. 주석·docstring·메시지 언어

| 무엇 | 언어 | 내용 |
|---|---|---|
| 모듈 docstring | 한국어 | 첫 줄 = 이 모듈의 역할(근거 문서 id — plan·ADR·PD — 가 있으면 함께). 다음 줄부터 계약(불변식·단위·실패 시 동작·허용 손실) |
| 클래스·함수 docstring | 한국어 | **왜**와 계약만. 이름·시그니처로 알 수 있는 "무엇"은 쓰지 않는다 |
| 줄 주석 | 한국어 | 코드만 보고는 알 수 없는 이유(예: `# 값 없는 BAD는 마지막 값을 지우지 않는다(품질만 바뀜).`) |
| OpenAPI 설명(`Field(description=…)`) | 한국어 | 계약 생성물(`core.json`·`schema.d.ts`)에 그대로 들어간다 |
| CLI `--help`(argparse `help=`) | 한국어 | 운영자가 읽는다 |
| 로그 메시지 | 영어 | 운영 grep·외부 도구용. `log.warning("cannot load definitions (will retry): %s", exc)` |
| 예외 메시지·CLI stdout/stderr | 영어 | `f"{key} must be an integer, got {raw!r}"` |
| UI 문구 | 한국어 | 운영자용 화면 텍스트 |
| `.importlinter` 등 ASCII 강제 파일 | 영어 | 한국어 Windows에서 cp949로 읽힌다 |

- 주석이 코드를 그대로 다시 말하면 지운다. 주석이 길어지면 이름이나 구조로 표현할 수 없는지 먼저 본다.
- 모듈 docstring이 없는 기존 파일은 손댈 때 채운다.
- TODO·FIXME로 미완성을 남기지 않는다. 남는 일은 PR 본문 백로그 표에 쓴다.
- `print`는 CLI 진입점(`cli.py`·`main.py`·`if __name__ == "__main__"` 단독 스크립트)에서만 쓰고, 라이브러리 코드는 `log`를 쓴다. 기계가 읽는 출력은
  `--json` 옵션으로 `json.dumps(..., sort_keys=True)`를 낸다.

## 8. 에러 처리·로깅·비동기

- 모듈마다 기반 예외 하나를 두고 세부 예외를 파생한다. 다른 모듈이 잡아야 하는 예외는 `ports.py`에 둔다.
- 변환할 때는 `raise NewError(...) from exc`(원인 보존) 또는 `from None`(원인이 잡음일 때)을 명시한다.
  맨 `except:`와 이유 없이 예외를 삼키는 `pass`는 금지다.
- 넓은 `except Exception:`(정리 경로에서는 `BaseException`)은 아래 셋에서만 쓴다.
  1. **오래 도는 loop·task 경계** — 한 번의 실패가 수집·flush·이벤트 전달 전체를 멈추지 않게 로그를 남기고
     계속한다(예: `historian/writer.py` flush loop, `platform/bus.py` 핸들러 호출). 반복될 수 있는 실패는
     traceback을 첫 실패·상태가 바뀔 때만 내고(`log.exception`), 이후는 `log.warning`이나 간격을 둔다.
  2. **변환 후 재던짐** — `raise MigrationFailedError(...) from exc` (`platform/migrate.py`).
  3. **정리 후 재던짐 또는 명시적 best-effort** — 자원을 정리하고 원래 예외를 다시 던지거나(`platform/db.py`),
     실패해도 되는 작업을 `contextlib.suppress(Exception)` 또는 `log` 후 계속으로 처리한다(updater `phases.py`
     gc). best-effort인 이유는 주석이나 docstring에 적는다.
- 일시적 실패(DB·collector 단절)는 **로그 + 재시도 + 상태 노출**(`db_ok`, `/readyz`)로 다루고 프로세스를
  죽이지 않는다(graceful degradation). 설정 오류처럼 고칠 수 없는 것은 기동 시 바로 실패한다(`SettingsError`).
- 로거는 모듈마다 `log = logging.getLogger(__name__)`, 인자는 `%s` 지연 포맷으로 넘긴다(f-string 금지).
  반복되는 경고는 간격을 두고 낸다(예: `DROP_WARN_INTERVAL_S`).
- 모든 외부 I/O에는 제한 시간을 건다(`asyncio.timeout`, DB 쿼리 `timeout`, `subprocess` `timeout`). 제한 값은
  이름 붙은 상수다.
- 백그라운드 task는 소유자가 `start()`/`stop()`으로 만들고 거둔다. 고아 task 금지.
- **임계 구역**: `TagStore`처럼 공유 상태를 바꾸는 메서드는 동기(`await` 없음)로 써서 이벤트 루프 안에서
  끊기지 않게 하고, 바뀐 결과(이벤트 목록)를 **반환**한다. DB I/O·이벤트 발행은 호출자가 그 뒤에 한다
  (불변 규칙 Lock discipline).

## 9. 테스트

- 파일은 `tests/test_<모듈>.py`, 모듈 docstring 첫 줄에 이 파일이 증명하는 것을 쓴다(빠진 기존 파일은 손댈 때).
- 테스트 이름은 행동을 말한다: `test_columns_convert_ns_to_us`.
- 가짜 객체는 **직접 쓴 작은 클래스**(`FakeStore`, `Clock`)로 Protocol을 만족시킨다. `unittest.mock`은 쓰지
  않는다 — 가짜가 Protocol과 어긋나면 pyright가 잡게 하기 위해서다.
- `monkeypatch` 허용: 환경변수, 표준 라이브러리·서드파티 함수(`os.*`, `sys.stdin`, `asyncpg.create_pool` 등).
  금지: 자기 코드의 모듈 속성·private 메서드 치환 → 그 값을 주입 인자로 바꾼다.
- 기대값은 구현에서 계산하지 않고 독립적으로 적는다(손으로 계산한 값·고정 예시).
- DB가 필요한 테스트는 `@pytest.mark.integration`(compose에서만 실행), 부하 측정은 `perf`. 무조건 `skip`·
  `.only`·빈 테스트로 통과시키지 않는다. 환경 조건부 `skipif`(OS·외부 도구 유무)는 `reason`을 적고 허용한다.

## 10. 프론트엔드 (`services/web`, 처방 — M3b부터 적용)

- API 타입은 `packages/contracts/openapi/core.json`에서 생성한 `src/api/schema.d.ts`만 쓴다(`openapi-fetch`).
  응답 타입을 손으로 다시 선언하지 않는다.
- 비동기 화면 상태는 판별 유니온으로 표현한다: `{ kind: "loading" } | { kind: "ok"; value } | { kind: "error"; message }`.
- `any`·non-null `!` 대신 타입 좁히기(`instanceof`, `in`, 판별 필드)를 쓴다. `as` 단언은 TS가 표현하지 못하는
  경우(예: `Object.keys` 결과를 키 유니온으로)만 허용한다.
- 화면은 `src/pages/<Name>.tsx`, WebSocket은 `src/ws/`, 재사용 컴포넌트는 두 번째 사용처가 생길 때 분리한다(4절).
- 상수는 단위 접미사를 붙인다(`POLL_MS`). effect는 정리 함수(`AbortController`·`clearInterval`)를 반드시 돌려준다.
- 상태 관리·UI 라이브러리 도입은 필요가 생긴 PR에서 근거와 함께 결정한다(legacy [frontend](frontend.md)의
  Jotai·singleton WS 등을 자동으로 가져오지 않는다).

## 11. 리뷰 체크리스트 (가독성·구조)

리뷰 워커는 정확성 검토와 **별도로** 이 항목을 확인하고, 어긋나면 심각도(대개 LOW·MEDIUM)와 함께 적는다.
12절 백로그에 있는 기존 위반은 새로 지적하지 않는다(그 파일을 고친 PR이면 맞췄는지 본다).

- [ ] 새 모듈이 3절 구조와 import-linter 세 계약에 맞게 등록됐는가
- [ ] 새 Protocol·추상화가 4절의 네 경우 중 하나인가, 쓰이지 않는 옵션·확장점은 없는가
- [ ] 가변 상태의 소유자가 하나이고 밖으로는 frozen 스냅샷을 내는가(3절)
- [ ] 이름이 5절을 따르는가(특히 단위 접미사, `Pg` 접두, `…Error`)
- [ ] 함수·파일이 한 문장으로 설명되는가(6절), 모듈 docstring 첫 줄이 그 문장인가
- [ ] 주석이 "무엇"을 반복하지 않고 "왜"·계약을 말하는가, 언어가 7절 표와 맞는가
- [ ] 넓은 catch가 8절의 세 경우인가, I/O에 이름 붙은 제한 시간이 있는가, 임계 구역 안에 await·I/O·발행이 없는가
- [ ] 가짜 객체가 직접 쓴 클래스이고, monkeypatch가 자기 코드를 치환하지 않고, 기대값이 독립적인가(9절)
- [ ] `print`는 CLI 진입점에만 있고 백그라운드 task에 소유자가 있는가(7·8절)
- [ ] (web) 생성 타입만 쓰고, `any`·`!`·불필요한 `as`가 없고, effect가 정리 함수를 돌려주는가(10절)
- [ ] 같은 일을 하는 기존 코드와 모양이 같은가(1절 4번)

## 12. 현재 코드와의 차이 (백로그, 2026-10-11 검토 기준)

규칙은 맞고 기존 코드 일부가 어긋나는 곳이다. 그 파일을 고치는 PR에서 함께 맞춘다.

| 규칙 | 위치 | 내용 |
|---|---|---|
| 8절 제한 시간 상수 | `services/updater/src/pureiron_updater/images.py` | `timeout=60/1800/30/120` 리터럴 → 이름 붙은 상수 |
| 9절 monkeypatch | `services/updater/tests/test_cli.py`(`cli.real_deps`), `deploy/tests/test_build_bundle.py`(`SIZE_LIMIT`), `packages/bundle-tools/tests/test_pib.py`·`test_keys_images_cli.py`(`BoundedTarInfo._base_proc_member`), `services/updater/tests/test_phases.py`(`pureiron_updater.gc.collect`) | 자기 코드 치환 → 주입 인자 |
| 2절 예외 이유 주석 | 루트 `pyproject.toml` `"**/tests/**" = ["PLR0915"]` | 이유 주석 없음 |
| 8절 로거 이름 | `getLogger("pureiron_updater")`, `getLogger("simulator")` | `__name__`으로 |
| 5절 크기 단위 접미사 | `SIZE_LIMIT`(`deploy/bundle/build_bundle.py`), `METADATA_LIMIT`(`pureiron_bundle/pib.py`), `DEFAULT_LIMIT`(`historian/reader.py`, 행 수) | `_BYTES`·`_ROWS` 접미사 |
| 5절 환경변수 접두 | updater `PUREIRON_POLL_INTERVAL_S`·`PUREIRON_CHECK_TIMEOUT_S`(서비스 전용인데 접두 없음) | **변경하지 않는다** — 현장 `.env`·compose와 맞물린 배포 계약 |
| 7·9절 docstring | 모듈 docstring 없음: `services/control-api/src/pureiron_control/app.py`; 테스트 docstring 없음 17개(예: collector `test_modbus.py`·`test_plan.py`) | 손댈 때 채움 |

## 13. 이 문서를 바꾸는 법

코드가 이 문서와 달라져야 할 이유가 생기면 코드를 먼저 바꾸지 말고 이 문서를 PR로 고친다(이유 포함). 규칙을
바꾸는 PR은 기존 코드를 함께 맞추거나, 맞추지 않으면 12절에 적는다.
