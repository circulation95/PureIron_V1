# Architecture — legacy projection

이 파일은 과거 아키텍처 본문을 담던 문서였지만, 현재 PureIron의 아키텍처 정본은 Wiki SOT로 이동했다.

- 시스템 구조/레이어/경계: [`docs/wiki/sot/architecture.md`](wiki/sot/architecture.md)
- DB·설정 계약: [`docs/wiki/sot/data-contracts.md`](wiki/sot/data-contracts.md)
- API·이벤트 계약: [`docs/wiki/sot/api-events.md`](wiki/sot/api-events.md)
- 런타임 설정/env: [`docs/wiki/sot/runtime-config.md`](wiki/sot/runtime-config.md)
- 아키텍처 결정 근거: [`docs/wiki/adr/index.md`](wiki/adr/index.md)

## 편집 규칙

아키텍처 상세를 이 파일에 다시 작성하지 않는다. 변경이 필요하면 해당 Wiki SOT를 수정한 뒤 다음을 실행한다.

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```
