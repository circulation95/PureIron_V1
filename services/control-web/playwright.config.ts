// control-web e2e(M4-3 G8): deploy/central compose(control-db·control-api·control-web=Caddy)를 실제로 띄우고
// chromium으로 Caddy basic auth → `/ui/fleet` → 표를 본다. 실행: `bash scripts/g1.sh control-web-e2e`(이미지 빌드 포함).
// 결과·trace·화면은 artifacts/control-web-e2e/.
import { join } from "node:path";

import { defineConfig, devices } from "@playwright/test";

import { ARTIFACTS, BASE_URL } from "./e2e/stack";

const TEST_TIMEOUT_MS = 2 * 60 * 1000;
const EXPECT_TIMEOUT_MS = 10 * 1000;

export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: TEST_TIMEOUT_MS,
  expect: { timeout: EXPECT_TIMEOUT_MS },
  workers: 1,
  retries: 0,
  forbidOnly: true,
  outputDir: join(ARTIFACTS, "test-results"),
  reporter: [["list"], ["html", { open: "never", outputFolder: join(ARTIFACTS, "report") }]],
  use: {
    baseURL: BASE_URL,
    // Caddy `tls internal`(로컬 CA)는 브라우저가 모른다(계획 R5) — e2e만 인증서 검사를 끈다.
    // 루트 CA로 검증하는 경로는 준비 스크립트(Python, heartbeat 전송)가 따로 확인한다.
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
