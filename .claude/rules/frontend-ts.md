---
paths: ["frontend/**/*.ts", "frontend/**/*.tsx"]
---

# 프론트엔드(TypeScript) 작업 규칙

> Canonical은 [docs/wiki/sot/frontend.md](../../docs/wiki/sot/frontend.md)·[frontend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/CLAUDE.md).
> 이 파일은 경로 스코프용 투영본이므로 짧게 유지하고, 규칙 본문 변경은 SOT(`scripts/wiki.py build`)로 한다.

`frontend/**/*.{ts,tsx}`를 작성·수정할 때 다음을 지킨다:

- **No `as any` / No `@ts-ignore`** — 타입을 우회하지 않는다. 불가피하면 좁은 타입 가드나 `unknown` + 검증으로 푼다.
- **`tsc --noEmit` 통과 필수** — 변경 후 `npm run lint`(= `tsc --noEmit`)가 깨끗해야 한다.
- **기존 패턴 재사용** — 새 전역 상태/스토어를 즉흥 도입하지 말고, 이미 있는 컴포넌트·훅·상태 구조를 먼저 쓴다.
- **데이터 계약 준수** — 백엔드 응답·이벤트 형태는 SOT [data-contracts](../../docs/wiki/sot/data-contracts.md)·[api-events](../../docs/wiki/sot/api-events.md)를 따른다. 임의 변형 금지.

## 마무리 체크

- 변경 후 `npm run lint` 통과(타입 에러 0).
