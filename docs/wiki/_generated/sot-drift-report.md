# SOT Drift Report

> 생성물(build 전용). drift 는 경고(warn-only)이며 빌드/커밋을 막지 않는다.

## env drift (`config.py` ↔ `.env.example`)

- (누락 없음)

`config.py` 에 없는 `.env.example` 변수: (없음)

## API drift (incomplete heuristic, v1)

router.py APIRouter prefix:
- (없음)

api-events.md 에 미기재된 소스 프리픽스: (없음)

문서에만 있고 소스에서 못 찾은 프리픽스:
- /api/alarms
- /api/auth
- /api/chat
- /api/dashboard
- /api/devices
- /api/diagnostics
- /api/docs
- /api/docs/*
- /api/history
- /api/logs
- /api/logs/{date}
- /api/mes
- /api/models
- /api/operational-events
- /api/settings
- /api/tags
- /api/udt
- /api/v1
- /api/v1/devices
- /api/v1/ws
- /docs
- /metrics
- /readyz
- /redoc
- /ws
- /{date}
- /{date}/page

prefix 없는 router 모듈:
- (없음)

main.py 직접 라우트(raw):
- (없음)

## source warnings (secret-like 기본값)

- (없음)
