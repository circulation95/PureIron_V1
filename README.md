# PureIron SCADA

> **PureIron_V1** — Foundation 재설계의 새 코드 repo다. 지금은 위키·문서·도구만 있고 서비스 코드(`services/`·`packages/`·`deploy/`)는 M1부터 추가된다. 이 파일의 backend/frontend/docker 명령·`scripts/execute.py`(harness)는 legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final)에서만 동작한다.

중소 산업시설용 경량 오픈소스 SCADA: FastAPI 백엔드 + React 프론트엔드 + 3D 디지털 트윈 + AI 멀티에이전트 채팅. 상용 SCADA의 라이선스 비용 없이 실시간 태그 모니터링, 알람, 이력, 스크립팅, OPC-UA·Modbus·S7 디바이스 운영을 제공한다.

## 빠른 시작

사전 요구: Python 3.12+ · [uv](https://github.com/astral-sh/uv) · Node.js 20+ · (선택) Docker.

### 백엔드

```bash
cd backend
uv sync
uv run uvicorn pureiron.main:app --reload --port 8000
```

API: `http://localhost:8000` · 대화형 문서: `http://localhost:8000/docs`

### 프론트엔드

```bash
cd frontend
npm install
npm run dev      # http://localhost:3000
```

### Docker (read-only PoC / 개발 스택)

고객 사이트 PoC 기본값은 read-only 안전 모드다. `.env.poc.example`은 `PUREIRON_POC_READ_ONLY_MODE=true`를 기본으로 두며, PoC compose는 Postgres/Redis를 호스트 포트로 노출하지 않고 시뮬레이터도 시작하지 않는다.

```bash
cp .env.poc.example .env.poc
# .env.poc의 CHANGE-ME 값 교체 후
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml up -d --build
```

개발/데모용 전체 스택은 별도 compose를 사용한다.

```bash
docker compose -f docker/docker-compose.yml up --build
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up --build   # dev override
```

상세 PoC 절차: [docs/runbooks/on-prem-read-only-poc.md](docs/runbooks/on-prem-read-only-poc.md)

기본 계정(개발용): `admin` / `admin`

## 테스트

```bash
cd backend && uv run pytest tests/ -v          # 단위/통합
cd backend && uv run pytest ../tests/e2e/ -v    # E2E (AC-1~10, NF-1~4)
cd frontend && npm run lint                      # tsc --noEmit
python scripts/wiki.py check                     # 문서 SOT 검증
```

## 문서 (Wiki SOT)

모든 정본 문서는 위키 허브에 있다. 사람과 LLM 에이전트 모두 이 위키를 기준으로 삼는다.

- **[docs/wiki/](docs/wiki/index.md)** — 문서 허브(제품·아키텍처·런타임 설정·데이터 계약·API·프론트엔드·테스트·에이전트 규칙·ADR)
- 운영자용 상세 매뉴얼: [MANUAL.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/MANUAL.md)
- 에이전트/하니스 규칙: [AGENTS.md](AGENTS.md) · [CLAUDE.md](CLAUDE.md) (위키 투영본)

## 라이선스

MIT
