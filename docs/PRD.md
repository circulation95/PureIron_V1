# PRD — legacy projection

이 파일은 과거 PRD 본문을 담던 문서였지만, 현재 PureIron의 제품 정본은 Wiki SOT로 이동했다.

- 제품 정체성, 고객, v1 범위, 첫 계약 전략: [`docs/wiki/sot/product.md`](wiki/sot/product.md)
- 전체 문서 허브: [`docs/wiki/index.md`](wiki/index.md)
- Agent 작업 기준: [`AGENTS.md`](../AGENTS.md)

## 편집 규칙

제품 전략이나 MVP 범위를 이 파일에 다시 작성하지 않는다. 변경이 필요하면 `docs/wiki/sot/product.md`를 수정한 뒤 다음을 실행한다.

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```
