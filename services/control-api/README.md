# pureiron-control-api

중앙 control-api(`/api/v1`). 현장 등록·자격증명·heartbeat·DesiredState·보고·폐쇄망 반입·드래프트·fleet 조회를 맡는다(ADR-022, foundation plan §6.4, M4 plan C5~C8). 현장은 이것 없이도 동작한다.

- API 표면·인증 표·seq 규칙: [api-events](../../docs/wiki/sot/api-events.md) control-api 절(정본은 코드와 `packages/contracts/openapi/control-api.json`)
- 표·마이그레이션: [data-contracts](../../docs/wiki/sot/data-contracts.md) M4-0 절, 환경변수: [runtime-config](../../docs/wiki/sot/runtime-config.md)

```bash
# 개발 control-db(PostgreSQL 16) — 비밀번호 파일은 $PUREIRON_SECRETS_DIR/control_db_password
docker compose -p pureiron-control -f deploy/compose/control.dev.yml up -d --wait control-db
export PUREIRON_CONTROL_DB_DSN=postgresql://pureiron@127.0.0.1:25433/pureiron_control
export PUREIRON_CONTROL_DB_PASSWORD_FILE=$PUREIRON_SECRETS_DIR/control_db_password
uv run pureiron-control migrate up                      # 부팅 때 자동 실행하지 않는다
uv run pureiron-control token create --role admin --name ops   # 원문은 이때 한 번만
uv run pureiron-control site register state/enrollment.json   # = POST /sites
uv run pureiron-control serve
```

구조는 core와 같다: `runtime.py`(`ControlRuntime.build`) → `app.py`(`create_app(rt)`) → `api/`(라우터) → 도메인 모듈(`sites`·`credentials`·`reports`·`bundles`·`drafts`·`fleet`) → `platform/`. 경계는 `.importlinter`. 배포 compose(Caddy·이미지)는 M4-3 `deploy/central`, 현장 Updater 송신은 M4-1.
