# services/web

현장 운영 화면(Vite + React 19 + TypeScript strict). nginx 이미지(`pureiron/web`)가 정적 파일과 `/api` → core 프록시를 맡는다.

- **M3b**: 로그인 + 5개 화면(태그 현황, 알람+ACK·저널, 트렌드(uPlot), 장비 상태, 감사 로그), WS 실시간 갱신. 편집 화면은 두지 않는다(No config UI).
- 구조·상태 규칙·WS·e2e 정본: [frontend SOT 16절](../../docs/wiki/sot/frontend.md).

```bash
npm ci
npm run dev          # http://localhost:3000 (core: PUREIRON_CORE_URL, 기본 127.0.0.1:18000)
npm run typecheck && npm run lint && npm run build
bash ../../scripts/g1.sh web-e2e   # repo 루트 기준 실행 권장: 이미지 빌드 + compose + Playwright
npm run gen:api      # 계약 변경 후 schema.d.ts 재생성(contracts-drift가 검사)
```

`as any`·`@ts-ignore`·`eslint-disable` 금지(G1 no-suppressions).
