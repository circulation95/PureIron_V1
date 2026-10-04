# 심각도·Signature·Dedupe·Noise 기준

`gh-log-triage-pr` skill의 분류 기준 정본. SKILL.md 절차 4~6단계에서 사용한다.

## 심각도 분류

| 등급 | 기준 | 예 |
|---|---|---|
| **P0** | 데이터 손상, 보안 사고, 전체 서비스 중단 | historian 데이터 유실, secret 노출, gateway 기동 불가 |
| **P1** | 핵심 기능 장애, 인증/DB/API 지속 실패 | 로그인 전면 실패, asyncpg 연결 반복 실패, tag write 전면 5xx |
| **P2** | 특정 기능 장애, 복구 가능한 실패 | 특정 driver 재연결 루프, 특정 endpoint 간헐 500, 특정 pytest 실패 |
| **P3** | 경고, 개발 편의성, 추후 조사 | deprecation 경고, 느린 쿼리 경고, 원인 불명 1회성 오류 |

판단이 애매하면 한 단계 낮게 분류하고 본문에 "추정" 표시와 함께 상향 조건을 적는다.

## Noise 분류 (issue 금지, 보고서의 별도 섹션으로)

- 개발 서버 재시작/reload 메시지
- 사용자 취소(KeyboardInterrupt, client disconnect)
- 이미 알려진 harmless 경고(예: 선택 서비스 미설정 시 graceful degradation 로그 — DB/Redis/RAG/LangGraph 부재는 PureIron 설계상 정상)
- 1회성 transient 오류 중 후속 로그에서 자동 복구가 확인되는 것

## Signature 규칙

같은 문제를 하나로 묶는 단위:

- 같은 stack trace (최상위 예외 기준)
- 같은 endpoint + HTTP status
- 같은 exception class + message 골격
- 같은 failing test nodeid

### Signature hash 규격

`sha256(정규화된 signature 문자열)`의 **앞 12자(hex)**. 입력은 non-secret 정보만 사용한다:

```
<exception_class>|<normalized_message>|<top_frame_path>:<function>
또는
<method> <endpoint_path_normalized>|<status>
또는
pytest|<test_nodeid>
```

### 정규화 (필수 — 무인 일일 실행에서 signature 불안정 = 매일 중복 issue이므로 안전 속성)

hash 입력 전에 가변 토큰을 placeholder로 치환한다:

- 숫자열 → `<N>` (status code 등 의미 있는 숫자는 유지)
- hex 문자열(8자 이상)·UUID → `<HEX>` / `<UUID>`
- 타임스탬프 → `<TS>`
- 따옴표로 감싼 값 → `<VAL>`
- 경로 내 가변 세그먼트(ID·날짜) → `<SEG>`

**Tradeoff**: 과잉 정규화는 서로 다른 오류를 한 signature로 합칠 수 있고, 그 경우 두 번째 오류가 자기 issue를 갖지 못한다. 정규화는 위 목록까지만 하고, 메시지의 구조적 골격(어떤 모듈·어떤 동작 실패인지)은 보존한다.

## Dedupe 조회 절차

1. **Primary**: `gh issue list --label log-triage --state all --limit 200 --json number,title,body` 결과에서 `log-triage-signature: <hash>` 문자열을 **로컬에서** 매칭한다(GitHub 검색 인덱싱에 의존하지 않음).
2. **Secondary**(primary 불가 시): `gh issue search --search "\"log-triage-signature: <hash>\" in:body" --state all`.
3. 매칭되면 새 issue를 만들지 않고 기존 issue에 코멘트를 단다(아래 throttle).

### Comment throttle (일일 실행 스팸 방지)

- 기존 issue의 마지막 코멘트가 **3일 이내**면 코멘트를 생략하고 보고서에만 기록한다.
- 단, **심각도가 상승**했으면(예: P2→P1) 즉시 코멘트하고 label을 갱신한다.

### Caveats

- dedupe 조회 경로는 **첫 실제 triage 실행 전까지 미검증**이다. 첫 실행 결과로 확인한다.
- `log-triage` label issue가 200개를 넘게 누적되면 `--limit` 상향 또는 pagination이 필요하다.
