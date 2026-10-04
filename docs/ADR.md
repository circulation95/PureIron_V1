# ADR — legacy projection

이 파일은 과거 Architecture Decision Records 본문을 담던 문서였지만, 현재 ADR 정본은 Wiki로 이동했다.

- ADR 정본: [`docs/wiki/adr/index.md`](wiki/adr/index.md)
- 관련 작업 규칙: [`docs/wiki/sot/agent-rules.md`](wiki/sot/agent-rules.md)
- 전체 문서 허브: [`docs/wiki/index.md`](wiki/index.md)

## 편집 규칙

새 결정이나 기존 결정 변경은 이 파일에 작성하지 않는다. `docs/wiki/adr/index.md`에 `ADR-XXX` 형식으로 추가/수정한 뒤 다음을 실행한다.

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```
