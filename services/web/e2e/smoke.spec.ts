// 운영 화면 스모크: 로그인 → 5화면 핵심 요소(실제 core·WS·nginx 경로) → 알람 ACK 1회 → 감사 → 로그아웃 뒤 보호.
// 화면마다 찍은 스크린샷은 artifacts/web-e2e/screens/(PR 첨부용).
import { join } from "node:path";

import { expect, type Page, test } from "@playwright/test";

import { ALARM_ID, ARTIFACTS, E2E_PASSWORD, USERS } from "./stack";

const ALARM_WAIT_MS = 90 * 1000; // 알람 조건이 처음 성립하기까지 사인파 한 주기(~63s) 안
const TAGS_GOOD_WAIT_MS = 30 * 1000;

async function login(page: Page, username: string): Promise<void> {
  await page.getByLabel("아이디").fill(username);
  await page.getByLabel("비밀번호").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();
}

async function screenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(ARTIFACTS, "screens", `${name}.png`), fullPage: true });
}

test("엔지니어: 로그인 → 5화면 → 알람 ACK → 감사 → 로그아웃", async ({ page }) => {
  await test.step("보호 경로는 로그인으로 보내고, 로그인 뒤 원래 경로로 돌아온다", async () => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "PureIron 로그인" })).toBeVisible();
    await screenshot(page, "0-login");
    await login(page, USERS.engineer);
    await expect(page.getByRole("heading", { name: "태그 현황" })).toBeVisible();
    await expect(page.getByText("엔지니어")).toBeVisible();
  });

  await test.step("Overview: GOOD 태그가 보인다", async () => {
    await expect(page.locator(".quality.q-good").first()).toBeVisible({ timeout: TAGS_GOOD_WAIT_MS });
    await expect(page.getByText("실시간 연결됨")).toBeVisible();
    await screenshot(page, "1-overview");
  });

  await test.step("Alarms: 활성 알람이 보이고 ACK 1회 뒤 상태가 바뀐다", async () => {
    await page.getByRole("link", { name: /^알람/ }).click();
    const row = page.locator(`tr[data-alarm-id="${ALARM_ID}"]`);
    const ack = row.getByRole("button", { name: "ACK" });
    await expect(ack).toBeVisible({ timeout: ALARM_WAIT_MS });
    await expect(page.getByLabel(/미확인 알람 \d+개/)).toBeVisible();
    await screenshot(page, "2-alarms");
    await ack.click();
    // 활성이면 ACTIVE_ACKED로 남고, 이미 해제됐으면 NORMAL이 되어 목록에서 빠진다 — 어느 쪽이든 ACK 버튼이 없어진다.
    await expect(ack).toHaveCount(0);
    await page.getByRole("tab", { name: "저널" }).click();
    await expect(page.getByRole("cell", { name: USERS.engineer }).first()).toBeVisible();
  });

  await test.step("Trend: 차트 canvas가 보인다", async () => {
    await page.getByRole("link", { name: "트렌드" }).click();
    await expect(page.getByTestId("trend-chart").locator("canvas")).toBeVisible();
    await screenshot(page, "3-trend");
  });

  await test.step("Devices: 장비가 연결됨(CONNECTED)", async () => {
    await page.getByRole("link", { name: "장비" }).click();
    await expect(page.locator('[data-state="CONNECTED"]')).toHaveText(/연결됨/);
    await screenshot(page, "4-devices");
  });

  await test.step("Audit: alarm.ack 행이 보인다", async () => {
    await page.getByRole("link", { name: "감사 로그" }).click();
    await expect(page.getByRole("cell", { name: "alarm.ack", exact: true }).first()).toBeVisible();
    await screenshot(page, "5-audit");
  });

  await test.step("로그아웃 뒤 보호 경로는 로그인으로 리다이렉트된다", async () => {
    await page.getByRole("button", { name: "로그아웃" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await page.goto("/alarms");
    await expect(page).toHaveURL(/\/login\?next=%2Falarms$/);
    await expect(page.getByRole("heading", { name: "PureIron 로그인" })).toBeVisible();
  });
});

test("로그아웃 상태에서 보호 경로는 ?next= 한 단계로 로그인에 보내고, 로그인 뒤 그 경로로 돌아온다", async ({ page }) => {
  await page.goto("/alarms");
  await expect(page).toHaveURL(/\/login\?next=%2Falarms$/);
  await login(page, USERS.viewer);
  await expect(page).toHaveURL(/\/alarms$/);
});

test("조회자: ACK 버튼과 감사 로그가 보이지 않는다", async ({ page }) => {
  await page.goto("/alarms");
  await login(page, USERS.viewer);
  await expect(page.getByRole("heading", { name: "알람" })).toBeVisible();
  // 목록 로드가 끝난 뒤에 검사한다(로딩 중에는 버튼이 원래 없어 통과해 버린다).
  await expect(page.getByText(/개 · 미확인/)).toBeVisible();
  await expect(page.getByRole("button", { name: "전체 ACK" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "ACK", exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "감사 로그" }).click();
  await expect(page.getByRole("note")).toHaveText(/권한이 없습니다/);
});

test("잘못된 비밀번호는 고정 문구로 거부한다", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("아이디").fill(USERS.viewer);
  await page.getByLabel("비밀번호").fill("wrong-password");
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page.getByRole("alert")).toHaveText("아이디 또는 비밀번호가 올바르지 않습니다.");
});
