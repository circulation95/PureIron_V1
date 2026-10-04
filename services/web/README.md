# services/web

현장 운영 화면(Vite + React 19 + TypeScript strict). nginx 이미지(`pureiron/web`)가 정적 파일과 `/api` → core 프록시를 맡는다.

- **M1**: 현장 준비 상태(`/api/v1/readyz`) 화면 골격, OpenAPI 생성 타입(`src/api/schema.d.ts`) 소비.
- **M3b**: 로그인 + 5개 화면(태그 현황, 알람+ACK, 트렌드, 장비 상태, 감사 로그, D9). 편집 화면은 두지 않는다(No config UI).

```bash
npm ci
npm run dev          # http://localhost:3000 (core: PUREIRON_CORE_URL, 기본 127.0.0.1:18000)
npx tsc --noEmit && npx eslint . --max-warnings 0
npm run gen:api      # 계약 변경 후 schema.d.ts 재생성(contracts-drift가 검사)
```

`as any`·`@ts-ignore`·`eslint-disable` 금지(G1 no-suppressions).
