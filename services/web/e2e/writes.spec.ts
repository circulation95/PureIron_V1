// PLC 쓰기 화면(plan M3c-3 E9): 실제 core·collector·시뮬레이터 위에서 설정값 CONFIRMED, 조회자 버튼 없음,
// 허가 조건 거짓 → 검사 거부, 명령 확인 2단계 → 장비 수신. 실제 스택에서 결정적으로 만들 수 없는 결과
// (설정값 readback 없음 = "장비 수신, 값 미확인"(M3c-2 리뷰 LOW-3), 응답 유실 = "결과 모름")는 confirm 응답만
// 바꿔 화면 문구를 본다 — 그때 confirm 요청은 core에 닿지 않는다(판정 정본은 core 단위 테스트).
// 리뷰 회귀(M3c-3 HIGH-1·HIGH-2·LOW-1·LOW-4): 명령 더블클릭·Enter 연타는 2단계를 건너뛰지 않고, 보내는
// 중 Esc를 반복해도 대화상자가 남아 결과를 보이며, confirm 실패 응답(409·410·404·503)은 정해진 문구로 간다.
import { expect, type Locator, type Page, type Route, test } from "@playwright/test";

import type { WriteResult } from "../src/api/client";
import { setPermissive, USERS, WRITES } from "./stack";
import { login, screenshot } from "./ui";

const TAGS_GOOD_WAIT_MS = 30 * 1000;
const CONFIRM_ROUTE = "**/api/v1/writes/*/confirm";

function row(page: Page, path: string): Locator {
  return page.locator(`tr[data-tag-path="${path}"]`);
}

async function openOverview(page: Page, username: string): Promise<void> {
  await page.goto("/");
  await login(page, username);
  await expect(page.getByRole("heading", { name: "태그 현황" })).toBeVisible();
}

/** 행의 값·품질이 바뀔 때까지(허가 조건 coil을 바꾼 뒤 수집 주기를 기다린다). */
async function expectTag(page: Page, path: string, value: string): Promise<void> {
  const tag = row(page, path);
  await expect(tag.locator("td.num")).toHaveText(value, { timeout: TAGS_GOOD_WAIT_MS });
  await expect(tag.locator(".quality")).toHaveText("GOOD");
}

/** confirm 요청을 core로 보내지 않고 `reply`로 답한다. 돌려준 함수는 지금까지 받은 confirm 수. */
async function fakeConfirm(page: Page, reply: (route: Route, writeId: string) => Promise<void>): Promise<() => number> {
  let hits = 0;
  await page.route(CONFIRM_ROUTE, async (route) => {
    hits += 1;
    await reply(route, new URL(route.request().url()).pathname.split("/").at(-2) ?? "");
  });
  return () => hits;
}

function setpointResult(writeId: string, outcome: WriteResult["outcome"]): WriteResult {
  return {
    write_id: writeId,
    tag_id: 0,
    tag_path: WRITES.setpoint,
    kind: "setpoint",
    value: 10,
    before: null,
    outcome,
    readback: null,
    readback_ts: null,
    error: outcome === "WRITTEN" ? "readback unavailable" : null,
  };
}

async function prepare(page: Page, path: string, reason: string, value?: string): Promise<Locator> {
  await row(page, path).getByRole("button", { name: `${path} 쓰기` }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading")).toContainText(path);
  if (value !== undefined) await dialog.getByLabel("목표값").fill(value);
  await dialog.getByLabel("사유").fill(reason);
  await dialog.getByRole("button", { name: "검사" }).click();
  return dialog;
}

test("운전원: 설정값 쓰기 → 검사 통과 → 실행 → 적용 확인", async ({ page }) => {
  await openOverview(page, USERS.operator);
  await expect(page.getByText("쓰기 가능 2개")).toBeVisible();
  await expect(row(page, WRITES.setpoint).locator(".quality")).toHaveText("GOOD", { timeout: TAGS_GOOD_WAIT_MS });

  const dialog = await prepare(page, WRITES.setpoint, "e2e 설정값", "42.5");
  await expect(dialog.getByRole("list", { name: "실행 전 검사" }).locator("li.bad")).toHaveCount(0);
  await screenshot(page, "6-write-prepared");
  await dialog.getByRole("button", { name: "실행", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("적용 확인");
  await screenshot(page, "7-write-confirmed");
  await dialog.getByRole("button", { name: "닫기" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row(page, WRITES.setpoint).locator("td.num")).toHaveText("42.50");
});

test("조회자: 쓰기 버튼이 없다", async ({ page }) => {
  await openOverview(page, USERS.viewer);
  // 쓰기 대상 로드가 끝난 뒤에 검사한다(로딩 중에는 버튼이 원래 없어 통과해 버린다).
  await expect(page.getByText("쓰기 가능 0개")).toBeVisible();
  await expect(page.getByRole("button", { name: /쓰기$/ })).toHaveCount(0);
});

test("허가 조건 거짓: 명령 검사에서 거부되고 보내지 않는다", async ({ page }) => {
  setPermissive(false);
  await openOverview(page, USERS.operator);
  await expectTag(page, WRITES.permissive, "거짓");

  const dialog = await prepare(page, WRITES.command, "e2e 허가 조건 거짓");
  await expect(dialog.getByRole("alert")).toHaveText("검사를 통과하지 못해 보내지 않았습니다.");
  await expect(dialog.locator('[data-check="permissive"]')).toContainText("허가 조건 전부 참");
  await screenshot(page, "8-write-rejected");
});

test("허가 조건 참: 명령은 확인을 한 번 더 받고 장비 수신을 표시한다", async ({ page }) => {
  setPermissive(true);
  await openOverview(page, USERS.operator);
  await expectTag(page, WRITES.permissive, "참");

  const dialog = await prepare(page, WRITES.command, "e2e 운전 명령");
  await dialog.getByRole("button", { name: "실행", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("정말 실행합니까?");
  await dialog.getByRole("button", { name: "명령 실행" }).click();
  await expect(dialog.getByRole("status")).toContainText("장비 수신");
  await screenshot(page, "9-command-written");
});

test("결과 문구: 설정값 readback 없음은 '값 미확인', confirm 응답 유실은 '결과 모름'", async ({ page }) => {
  await openOverview(page, USERS.operator);
  await expect(row(page, WRITES.setpoint).locator(".quality")).toHaveText("GOOD", { timeout: TAGS_GOOD_WAIT_MS });

  let gatewayTimeout = false;
  const hits = await fakeConfirm(page, async (route, writeId) => {
    if (gatewayTimeout) await route.fulfill({ status: 504, body: "" });
    else await route.fulfill({ status: 200, json: setpointResult(writeId, "WRITTEN") });
  });
  let dialog = await prepare(page, WRITES.setpoint, "e2e 문구 확인", "10");
  await dialog.getByRole("button", { name: "실행", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("장비 수신, 값 미확인");
  await screenshot(page, "10-write-unverified");
  await dialog.getByRole("button", { name: "닫기" }).click();

  gatewayTimeout = true;
  dialog = await prepare(page, WRITES.setpoint, "e2e 문구 확인", "11");
  await dialog.getByRole("button", { name: "실행", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("응답 없음 — 결과를 알 수 없습니다");
  await expect(dialog.getByRole("alert")).toContainText("자동으로 다시 보내지 않습니다");
  await screenshot(page, "11-write-no-response");
  expect(hits()).toBe(2); // 두 confirm 모두 대체됐다(core에 가지 않음)
});

test("명령 실행 더블클릭·Enter 연타는 2단계 확인을 건너뛰지 않는다", async ({ page }) => {
  setPermissive(true);
  await openOverview(page, USERS.operator);
  await expectTag(page, WRITES.permissive, "참");
  const hits = await fakeConfirm(page, (route) => route.fulfill({ status: 500, body: "" }));

  let dialog = await prepare(page, WRITES.command, "e2e 더블클릭");
  await dialog.getByRole("button", { name: "실행", exact: true }).dblclick();
  await expect(dialog.getByRole("alert")).toContainText("정말 실행합니까?");
  await expect(dialog.getByRole("button", { name: "명령 실행" })).toBeEnabled(); // 잠깐 막혔다가 풀린다
  expect(hits()).toBe(0);
  await dialog.getByRole("button", { name: "취소" }).click();
  await expect(dialog).toHaveCount(0);

  dialog = await prepare(page, WRITES.command, "e2e Enter 연타");
  await dialog.getByRole("button", { name: "실행", exact: true }).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter"); // 포커스가 `취소`로 옮겨져 있어 닫힌다
  await expect(dialog).toHaveCount(0);
  expect(hits()).toBe(0);
});

test("보내는 중 Esc를 반복해도 대화상자가 남아 결과를 보인다", async ({ page }) => {
  await openOverview(page, USERS.operator);
  await expect(row(page, WRITES.setpoint).locator(".quality")).toHaveText("GOOD", { timeout: TAGS_GOOD_WAIT_MS });
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await fakeConfirm(page, async (route, writeId) => {
    await released;
    await route.fulfill({ status: 200, json: setpointResult(writeId, "NO_RESPONSE") });
  });

  const dialog = await prepare(page, WRITES.setpoint, "e2e Esc", "12");
  await dialog.getByRole("button", { name: "실행", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "실행 중…" })).toBeDisabled();
  for (let i = 0; i < 4; i += 1) await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  release();
  await expect(dialog.getByRole("alert")).toContainText("응답 없음 — 결과를 알 수 없습니다");
});

test("confirm 실패 응답: 409·410·404·503은 정해진 문구로 간다", async ({ page }) => {
  await openOverview(page, USERS.operator);
  await expect(row(page, WRITES.setpoint).locator(".quality")).toHaveText("GOOD", { timeout: TAGS_GOOD_WAIT_MS });
  let reply: { status: number; json: unknown } = { status: 500, json: {} };
  await fakeConfirm(page, (route) => route.fulfill(reply));

  const cases: { status: number; json: unknown; text: string; close: string }[] = [
    {
      status: 409,
      json: { detail: "write rejected by checks", write_id: null, checks: [{ name: "tag_good", ok: false, detail: "BAD" }] },
      text: "보내기 직전 검사에서 거부되어 보내지 않았습니다.",
      close: "닫기",
    },
    { status: 410, json: { detail: "expired" }, text: "확인 시간이 지났습니다. 다시 검사하세요.", close: "취소" },
    { status: 404, json: { detail: "not found" }, text: "이미 처리됐거나 없는 요청입니다.", close: "닫기" },
    {
      status: 503,
      json: { detail: "audit unavailable; write not sent" },
      text: "감사 기록에 실패해 장비로 보내지 않았습니다.",
      close: "닫기",
    },
    { status: 503, json: { detail: "upstream unavailable" }, text: "응답 없음 — 결과를 알 수 없습니다", close: "닫기" },
  ];
  for (const item of cases) {
    reply = { status: item.status, json: item.json };
    const dialog = await prepare(page, WRITES.setpoint, `e2e confirm ${String(item.status)}`, "13");
    await dialog.getByRole("button", { name: "실행", exact: true }).click();
    await expect(dialog.getByRole("alert")).toContainText(item.text);
    await dialog.getByRole("button", { name: item.close }).click();
    await expect(dialog).toHaveCount(0);
  }
});
