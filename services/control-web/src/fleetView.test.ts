// fleetView 판정 단위 표(브라우저 없음, `npm run unit`). 기대값은 계획 G7 문장에서 바로 옮긴다.
import { expect, test } from "@playwright/test";

import {
  attemptText,
  currentCell,
  failureMessage,
  formatTime,
  isLatest,
  siteView,
  timeZoneLabel,
  type Connection,
  type FleetSite,
  type LoadFailure,
} from "./fleetView";

const REPORTED_AT = "2026-11-01T09:00:00Z"; // 현장 시계
const LAST_SEEN = "2026-11-07T03:04:05Z"; // 중앙 시계

function site(overrides: Partial<FleetSite> = {}): FleetSite {
  return {
    site: "site-a",
    connection: "online",
    desired: {
      generation: 3,
      sha256: `sha256:${"a".repeat(64)}`,
      size: 10,
      product_version: "2026.11.0",
      project_version: "v3",
      published_at: "2026-11-01T00:00:00Z",
    },
    max_published_gen: 3,
    current: { product: "2026.11.0", project_version: "v3", gen: 3, reported_at: REPORTED_AT, source: "online" },
    last_seen: LAST_SEEN,
    last_attempt: null,
    disk_state: "ok",
    updater_phase: "idle",
    last_heartbeat_seq: 1,
    last_report_seq: 0,
    ...overrides,
  };
}

const NO_DESIRED = site({ desired: null, max_published_gen: null });

const LATEST_TABLE: { name: string; row: FleetSite; latest: boolean }[] = [
  { name: "online · current == desired → 최신", row: site(), latest: true },
  { name: "stale · 같은 gen이어도 최신 아님", row: site({ connection: "stale" }), latest: false },
  { name: "never · 최신 아님", row: site({ connection: "never", last_seen: null }), latest: false },
  {
    name: "online · current gen < desired → 최신 아님",
    row: site({ current: { product: "2026.11.0", project_version: "v2", gen: 2, reported_at: REPORTED_AT, source: "online" } }),
    latest: false,
  },
  {
    name: "online · current gen 없음(null) → 최신 아님",
    row: site({ current: { product: null, project_version: null, gen: null, reported_at: REPORTED_AT, source: "online" } }),
    latest: false,
  },
  {
    // 폐쇄망 반입으로 더 높은 gen이 적용됐거나 desired를 되돌린 경우 — "같을 때만" 최신(리뷰 L2).
    name: "online · current gen > desired → 최신 아님",
    row: site({ current: { product: "2026.11.0", project_version: "v4", gen: 4, reported_at: REPORTED_AT, source: "offline" } }),
    latest: false,
  },
  { name: "online · current 보고 없음 → 최신 아님", row: site({ current: null }), latest: false },
  { name: "online · 게시(desired) 없음 → 최신 아님", row: NO_DESIRED, latest: false },
];

for (const { name, row, latest } of LATEST_TABLE) {
  test(`isLatest: ${name}`, () => {
    expect(isLatest(row)).toBe(latest);
    expect(siteView(row).latest).toBe(latest);
  });
}

const CURRENT_TABLE: { connection: Connection; kind: "current" | "last-reported" }[] = [
  { connection: "online", kind: "current" },
  { connection: "stale", kind: "last-reported" },
  { connection: "never", kind: "last-reported" },
];

for (const { connection, kind } of CURRENT_TABLE) {
  test(`currentCell: ${connection} → ${kind}, 시각은 현장 reported_at`, () => {
    const cell = currentCell(site({ connection }));
    expect(cell).toEqual({ kind, version: "g3 · 2026.11.0 / v3", reportedAt: REPORTED_AT, source: "online" });
  });
}

test("currentCell: 보고가 없으면 none", () => {
  expect(currentCell(site({ connection: "never", current: null, last_seen: null }))).toEqual({ kind: "none" });
});

test("siteView: last_seen(중앙)과 current.reported_at(현장)을 따로 낸다", () => {
  const view = siteView(site({ connection: "stale" }));
  expect(view.lastSeen).toBe(LAST_SEEN);
  expect(view.current).toMatchObject({ kind: "last-reported", reportedAt: REPORTED_AT });
  expect(view.connectionLabel).toBe("응답 없음");
  expect(view.desired).toBe("g3 · 2026.11.0 / v3");
});

test("siteView: 게시 없음·디스크 상태 없음", () => {
  const view = siteView(site({ desired: null, disk_state: null }));
  expect(view.desired).toBe("게시 없음");
  expect(view.diskState).toBe("-");
});

test("attemptText: 결과·from→to·단계·코드, source는 따로", () => {
  const attempt = {
    report_seq: 4,
    source: "offline",
    received_at: LAST_SEEN,
    generation: 3,
    from: 2,
    to: 3,
    phase: "verifying",
    result: "failed",
    code: "E_CHECK",
    started_at: null,
    ended_at: null,
  };
  const row = site({ last_attempt: attempt });
  expect(attemptText(row)).toBe("failed · g2→g3 · 단계 verifying · 코드 E_CHECK");
  expect(siteView(row).attemptSource).toBe("폐쇄망 반입");
  const succeeded = { ...attempt, phase: "succeeded", result: "succeeded", code: null, source: "online" };
  expect(attemptText(site({ last_attempt: succeeded }))).toBe("succeeded · g2→g3");
  expect(attemptText(site())).toBeNull();
});

test("formatTime: 시간대를 넘기면 그 시간대의 24시간제", () => {
  expect(formatTime(REPORTED_AT, "Asia/Seoul")).toBe("2026-11-01 18:00:00");
  expect(formatTime(REPORTED_AT, "UTC")).toBe("2026-11-01 09:00:00");
  expect(formatTime("not-a-time")).toBe("not-a-time");
});

test("timeZoneLabel: 화면 시각 옆에 붙이는 시간대 표기", () => {
  expect(timeZoneLabel(new Date(REPORTED_AT), "Asia/Seoul")).toBe("GMT+9");
  expect(timeZoneLabel(new Date(REPORTED_AT), "Asia/Kolkata")).toBe("GMT+5:30");
  // 이름(EST·EDT)이 아니라 오프셋이다. Seoul·Kolkata는 en-US에서 둘이 같아 구분하지 못한다(리뷰 W3).
  // 일광 절약 시간 지역은 넘긴 시각의 오프셋이라 시각을 고정한다.
  expect(timeZoneLabel(new Date("2026-01-15T12:00:00Z"), "America/New_York")).toBe("GMT-5");
  expect(timeZoneLabel(new Date("2026-07-15T12:00:00Z"), "America/New_York")).toBe("GMT-4");
});

// 상태마다 그 상태에만 있는 문구까지 본다 — 분기를 지우면 기본 문구("현장 현황을 불러오지 못했습니다(HTTP n)")로
// 떨어지고 "HTTP n"만 보는 단언은 그것을 놓친다(리뷰 N1).
const FAILURE_TABLE: { failure: LoadFailure; contains: string[] }[] = [
  { failure: { kind: "http", status: 401 }, contains: ["HTTP 401", "인증에 실패", "화면 로그인", "viewer 토큰"] },
  { failure: { kind: "http", status: 403 }, contains: ["HTTP 403", "fleet 조회 권한"] },
  { failure: { kind: "http", status: 503 }, contains: ["HTTP 503", "중앙 DB"] },
  { failure: { kind: "http", status: 502 }, contains: ["응답하지 않습니다(HTTP 502)"] },
  { failure: { kind: "http", status: 418 }, contains: ["현장 현황을 불러오지 못했습니다(HTTP 418)"] },
  { failure: { kind: "network", detail: "Failed to fetch" }, contains: ["연결할 수 없습니다(Failed to fetch)"] },
  { failure: { kind: "format" }, contains: ["형식"] },
];

for (const { failure, contains } of FAILURE_TABLE) {
  test(`failureMessage: ${JSON.stringify(failure)}`, () => {
    const message = failureMessage(failure);
    for (const text of contains) expect(message).toContain(text);
  });
}
