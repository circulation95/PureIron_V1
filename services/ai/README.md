# services/ai

현장 AI 보조 서비스 자리. **첫 완료 범위에서 제외**한다(ADR-019).

- 계약: core의 **read-only** HTTP API(`packages/contracts/openapi/core.json`의 조회 경로)만 소비한다. 쓰기·제어 경로는 쓰지 않는다(ADR-013/014: 보조이지 자율 제어가 아니다).
- `ai`가 없어도 현장은 동작해야 한다(Graceful degradation). compose에 넣을 때도 선택 서비스다.
- legacy 에이전트 기능의 이식 순서는 plan §9(Deferred 기능 이식 순서)를 따른다.
