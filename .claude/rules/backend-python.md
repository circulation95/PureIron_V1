---
paths: ["backend/**/*.py"]
---

# 백엔드(Python) 작업 규칙

> Canonical은 [docs/wiki/sot/agent-rules.md](../../docs/wiki/sot/agent-rules.md)·[backend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md).
> 이 파일은 경로 스코프용 투영본이므로 짧게 유지하고, 규칙 본문 변경은 SOT(`scripts/wiki.py build`)로 한다.

`backend/**/*.py`를 작성·수정할 때 다음 불변 규칙을 지킨다:

- **No `app.state`** — `request.app.state` 금지. 모든 상태는 `gateway.*` 경유.
- **No ORM** — asyncpg raw SQL만. 스키마 변경은 멱등(idempotent) 마이그레이션으로.
- **Lock discipline** — TagStore 락 안에서 DB I/O·`tag.changed.*` 발행 금지(데드락 방지). 락은 짧게 잡고 I/O는 밖에서.
- **Event-driven** — 태그 변경은 `tag.changed.*` pub/sub으로 전파. 직접 호출로 우회하지 않는다.
- **Graceful degradation** — 선택적 서비스(DB/Redis/RAG/LangGraph)는 안전하게 실패. 부재가 전체 크래시로 번지지 않게 한다.
- **No plugin system** — OPC-UA·Modbus·S7는 하드코딩. 새 프로토콜은 `drivers/`에 직접 추가.
- **Device health is not a tag** — `DriverHealth.snapshot()`(`/api/devices`)이 단일 소스. `[health]` 시스템 태그를 코드로 생성하지 않는다.

## 마무리 체크

- 변경 후 `uv run ruff check .` 통과.
- 테스트 영향 시 `uv run pytest tests/ -v`.
