// web-e2e 스모크(plan M3b D14): compose 스택의 web(nginx 정적 + /api 프록시) 위에서 chromium으로 돈다.
// 실행: `bash scripts/g1.sh web-e2e`(이미지 빌드 포함). 결과·trace·화면은 artifacts/web-e2e/.
import { join } from "node:path";

import { defineConfig, devices } from "@playwright/test";

import { ARTIFACTS, WEB_PORT } from "./e2e/stack";

const TEST_TIMEOUT_MS = 4 * 60 * 1000; // 알람이 처음 성립하기까지 사인파 반 주기(~32s)를 기다릴 수 있다
const EXPECT_TIMEOUT_MS = 15 * 1000;

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
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
