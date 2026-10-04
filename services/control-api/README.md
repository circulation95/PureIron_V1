# pureiron-control-api

중앙 control-api(`/api/v1`). 현장 등록·자격증명·heartbeat·DesiredState·보고·fleet 조회를 맡는다(ADR-022, plan §6.4).

- **범위: M4** — 현장 Ed25519 키 등록, token, 번들 게시(gen ≤ 최신 409), 보고 `report_seq` 규칙.
- M1에서는 OpenAPI 계약 자리(`GET /api/v1/healthz`)만 두어 `contracts-diff`·`contracts-drift` 게이트가 처음부터 control-api 표면을 추적하게 한다.
