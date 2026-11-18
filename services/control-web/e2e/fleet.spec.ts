// control-web e2e(M4-3 G8): 실제 deploy/central 스택(Caddy·control-api·control-db, poll_s = 2 → stale 6s).
// 순서가 의미 있다(serial): 경로 규칙 → 응답에 토큰 없음 → 화면 로드(수신 없음) → heartbeat 1건 → 새로고침 → online·최신
// → 약 7초 뒤 stale(될 때까지 새로고침, 상한 있음)·마지막 보고값 → 오류 표시.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

import { readState, type StackState } from "./stack";

test.describe.configure({ mode: "serial" });

const SECURITY_HEADERS: Record<string, string> = {
  "content-security-policy": "frame-ancestors 'none'",
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
  "referrer-policy": "same-origin",
};

let stack: StackState;
let anonymous: APIRequestContext;
let operator: APIRequestContext;

test.beforeAll(async () => {
  stack = readState();
  anonymous = await request.newContext({ baseURL: stack.base_url, ignoreHTTPSErrors: true });
  operator = await request.newContext({
    baseURL: stack.base_url,
    ignoreHTTPSErrors: true,
    httpCredentials: { username: stack.user, password: stack.password, send: "always" },
  });
});

test.afterAll(async () => {
  await anonymous.dispose();
  await operator.dispose();
});

function siteRow(page: Page) {
  return page.locator(`tr[data-site="${stack.site}"]`);
}

/** "2026-11-01T09:00:00+00:00" → 화면 시각(Asia/Seoul, playwright.config.ts timezoneId). */
function seoul(iso: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

test("basic auth 없으면 UI·/ui/fleet은 401, /api/v1은 basic auth 없이 control-api로(G3)", async () => {
  for (const path of ["/", "/index.html", "/ui/fleet", "/ui/other"]) {
    const response = await anonymous.get(path);
    expect(response.status(), path).toBe(401);
    expect(response.headers()["www-authenticate"], path).toContain("Basic");
  }
  const health = await anonymous.get("/api/v1/healthz");
  expect(health.status()).toBe(200);
  // 브라우저 basic auth 자격은 API 권한이 아니다: control-api는 Bearer만 본다(토큰은 /ui/fleet에서만 붙는다).
  expect((await operator.get("/api/v1/fleet")).status()).toBe(401);
  expect((await anonymous.get("/api/v1/fleet")).status()).toBe(401);
});

test("보안 헤더 4개가 정적·프록시·오류 응답에 모두 붙는다", async () => {
  const responses = [
    await operator.get("/"),
    await operator.get("/ui/fleet"),
    await anonymous.get("/api/v1/healthz"),
    await anonymous.get("/"),
  ];
  for (const response of responses) {
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
      expect(response.headers()[name], `${response.url()} ${String(response.status())} ${name}`).toBe(value);
    }
  }
});

test("viewer 토큰 주입은 GET /ui/fleet 하나뿐 — 다른 /ui/* 경로·메서드는 404(R1)", async () => {
  const fleet = await operator.get("/ui/fleet");
  expect(fleet.status()).toBe(200);
  const body: unknown = await fleet.json();
  expect(Array.isArray(body)).toBe(true);
  for (const path of ["/ui/other", "/ui/fleet/", "/ui/fleet/x", "/ui/api/v1/fleet", "/ui/"]) {
    expect((await operator.get(path)).status(), path).toBe(404);
  }
  for (const method of ["POST", "PUT", "DELETE"] as const) {
    expect((await operator.fetch("/ui/fleet", { method })).status(), method).toBe(404);
  }
});

test("브라우저가 받는 응답 헤더·본문에 viewer 토큰 원문이 없다(★G1)", async () => {
  const token = readFileSync(join(stack.work, "secrets", "viewer_token"), "utf-8").trim();
  expect(token).not.toBe("");
  expect(token).not.toBe("not-issued-yet"); // central_stack.py가 발급 전에 넣어 두는 값
  for (const path of ["/", "/ui/fleet"]) {
    const response = await operator.get(path);
    expect(response.status(), path).toBe(200);
    expect(JSON.stringify(response.headersArray()), `${path} 헤더`).not.toContain(token);
    expect(await response.text(), `${path} 본문`).not.toContain(token);
  }
});

test("heartbeat 1건 → online·최신, 약 7초 뒤 stale·마지막 보고값·최신 없음", async ({ page, context }) => {
  await context.setHTTPCredentials({ username: stack.user, password: stack.password });
  const staleAfterMs = 3 * stack.poll_s * 1000; // C8: online = last_seen <= 3 x poll
  const refresh = page.getByRole("button", { name: "새로고침" });

  // 화면을 먼저 띄운다(heartbeat 전이라 "수신 없음"). 첫 화면 로드 시간이 6초 online 창을 깎지 않게(리뷰 L7).
  await page.goto("/");
  const row = siteRow(page);
  const current = row.locator('td[data-col="current"]');
  await expect(row).toHaveAttribute("data-connection", "never");
  await expect(refresh).toBeEnabled();

  const sent = await anonymous.post(`/api/v1/sites/${stack.site}/heartbeat`, {
    headers: { Authorization: `Bearer ${stack.site_token}`, "Content-Type": "application/json" },
    data: readFileSync(stack.heartbeat_file),
  });
  expect(sent.status(), await sent.text()).toBe(200);
  const sentAt = Date.now();
  await refresh.click();
  // online 판정은 6초 창 안에서만 참이다 — 남은 창 안에서만 기다린다(새로고침 조회 한 번이면 된다).
  const onlineWindowMs = staleAfterMs - (Date.now() - sentAt);
  expect(onlineWindowMs).toBeGreaterThan(0);
  await expect(row).toHaveAttribute("data-connection", "online", { timeout: onlineWindowMs });
  await expect(row).toContainText("연결됨");
  await expect(current.locator(".badge-latest")).toHaveText("최신");
  await expect(current).not.toContainText("마지막 보고값");
  await expect(row).toContainText(`g${String(stack.generation)} · 2026.11.0 / v1`);
  // 현장 보고 시각(현장 시계)과 중앙 수신 시각(중앙 시계)은 이름과 칸이 다르다.
  await expect(current).toContainText(`현장 보고 시각 ${seoul(stack.reported_at)}`);
  const lastSeen = row.locator('td[data-col="last-seen"]');
  await expect(lastSeen).not.toHaveText(seoul(stack.reported_at));
  await expect(page.locator("thead")).toContainText("중앙 수신 시각");
  await page.screenshot({ path: test.info().outputPath("online.png"), fullPage: true });

  // 중앙은 벽시계로 판정하고(last_seen·now 모두 datetime.now) 시험은 단조 시계로 기다린다. CI의 WSL VM 벽시계가
  // 기다리는 사이 1~2초 뒤로 당겨져 첫 새로고침이 online인 적이 있다(run 43731793847·44114140689) — 그래서
  // 새로고침 → 판정을 되풀이한다. 상한은 stale 창 하나 더: 끝내 stale이 아니면 실패한다.
  const waitMs = staleAfterMs + 1000 - (Date.now() - sentAt);
  if (waitMs > 0) await page.waitForTimeout(waitMs);
  await expect(async () => {
    await refresh.click();
    await expect(row).toHaveAttribute("data-connection", "stale", { timeout: 1000 });
  }).toPass({ timeout: staleAfterMs });
  await expect(row).toContainText("응답 없음");
  await expect(current).toContainText("마지막 보고값");
  await expect(current).toContainText(`현장 보고 시각 ${seoul(stack.reported_at)}`);
  await expect(current.locator(".badge-latest")).toHaveCount(0);
  await expect(page.getByText("최신", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("stale.png"), fullPage: true });
});

test("조회 실패는 표 대신 오류 문구(401·503·네트워크)", async ({ page, context }) => {
  await context.setHTTPCredentials({ username: stack.user, password: stack.password });
  const cases: { fulfill: number | "abort"; text: string }[] = [
    { fulfill: 401, text: "HTTP 401" },
    { fulfill: 503, text: "HTTP 503" },
    { fulfill: "abort", text: "중앙 서버에 연결할 수 없습니다" },
  ];
  for (const { fulfill, text } of cases) {
    await page.unrouteAll();
    await page.route("**/ui/fleet", (route) =>
      fulfill === "abort" ? route.abort("connectionrefused") : route.fulfill({ status: fulfill, body: "" }),
    );
    await page.goto("/");
    await expect(page.getByRole("alert")).toContainText(text);
    await expect(page.locator("table")).toHaveCount(0);
  }
});
