// 단위 표(브라우저·스택 없음): 판정 순수 함수(src/fleetView.ts)를 Playwright 러너로 돌린다 —
// site web과 같은 도구체인에 단위 테스트 도구를 더하지 않는다(M4-3 계획 §2-1). `npm run unit`.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "src",
  testMatch: "**/*.test.ts",
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
});
