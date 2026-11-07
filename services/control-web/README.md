# pureiron-control-web

중앙 현장 현황 화면(M4-3). 읽기 전용 표 1장 — 현장별 연결 상태·목표(desired)·현재(current)·중앙 수신 시각·마지막 적용 시도·디스크 상태. 30초 자동 + 수동 새로고침, 편집 기능 없음.

- **브라우저는 토큰을 갖지 않는다**(★G1). 사람 관문은 Caddy basic auth이고, 화면은 `GET /ui/fleet` 하나만 부른다 — Caddy가 그 경로에만 viewer 토큰(비밀 파일)을 붙여 control-api `GET /api/v1/fleet`로 넘긴다. 배포·경로 규칙은 [deploy/central](../../deploy/central/README.md).
- 판정(연결·"최신" 배지·"마지막 보고값")은 순수 함수 `src/fleetView.ts`, 단위 표는 `src/fleetView.test.ts`(`npm run unit`, 브라우저 없음).
- 타입 정본은 `packages/contracts/openapi/control-api.json` → `src/api/schema.d.ts`(`bash scripts/g1.sh contracts-gen`, 수기 편집 금지).
- e2e: `bash scripts/g1.sh control-web-e2e` — deploy/central compose를 실제로 띄운다(결과 `artifacts/control-web-e2e/`).

```bash
npm ci
# 개발 서버(:3100): /ui/fleet → control-api(:8100)의 /api/v1/fleet, viewer 토큰은 파일에서
PUREIRON_CONTROL_VIEWER_TOKEN_FILE=/path/to/viewer_token npm run dev
npm run typecheck && npm run lint && npm run unit && npm run build
```

화면 규칙과 용어는 위키 [frontend](../../docs/wiki/sot/frontend.md) control-web 절.
