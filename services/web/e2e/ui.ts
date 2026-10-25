// e2e 화면 공통 동작: 로그인 폼 입력, 스크린샷(artifacts/web-e2e/screens/, PR 첨부용).
import { join } from "node:path";

import type { Page } from "@playwright/test";

import { ARTIFACTS, E2E_PASSWORD } from "./stack";

export async function login(page: Page, username: string): Promise<void> {
  await page.getByLabel("아이디").fill(username);
  await page.getByLabel("비밀번호").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();
}

export async function screenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(ARTIFACTS, "screens", `${name}.png`), fullPage: true });
}
