// fleet 표 판정(순수 함수, M4-3 G7). 화면(FleetTable)은 이 결과를 그리기만 한다.
//
// - 연결 상태(`connection`)는 control-api가 응답 때 중앙 시각으로 계산한다(online ≤ 3×poll, C8).
//   화면은 다시 계산하지 않는다 — 브라우저 시계는 판정에 쓰지 않는다.
// - "최신" 배지는 online이고 현재 gen == 목표(desired) gen일 때만. stale·never면 current는
//   "마지막 보고값"(그 값을 현장이 보고한 현장 시각 `reported_at`)이고 배지가 없다.
// - 두 시각은 이름을 나눈다: `reported_at` = 현장 보고 시각(현장 시계), `last_seen` = 중앙 수신
//   시각(중앙 시계). 같은 열에 섞지 않는다(M4-0 보고서 5-#6).
import type { components } from "./api/schema";

export type FleetSite = components["schemas"]["FleetSite"];
export type Connection = components["schemas"]["ConnectionState"];

export const CONNECTION_LABELS: Record<Connection, string> = {
  online: "연결됨",
  stale: "응답 없음",
  never: "수신 없음",
};

export const LABELS = {
  latest: "최신",
  lastReported: "마지막 보고값",
  reportedAt: "현장 보고 시각",
  lastSeen: "중앙 수신 시각",
  noDesired: "게시 없음",
  noCurrent: "보고 없음",
} as const;

export const SOURCE_LABELS: Record<string, string> = { online: "온라인 수신", offline: "폐쇄망 반입" };

/** 표 한 줄의 current 칸. `last-reported` = 연결이 끊긴 현장의 마지막 보고값. */
export type CurrentCell =
  | { kind: "none" }
  | { kind: "current" | "last-reported"; version: string; reportedAt: string; source: string };

export interface SiteView {
  site: string;
  connection: Connection;
  connectionLabel: string;
  latest: boolean;
  desired: string;
  current: CurrentCell;
  /** 중앙 수신 시각(ISO). 없으면 null(한 번도 온라인 메시지를 받지 않음). */
  lastSeen: string | null;
  attempt: string | null;
  attemptSource: string | null;
  diskState: string;
}

function dash(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

/** "g3 · 2026.11.0 / v3" — gen · 제품 버전 / 프로젝트 버전. */
export function versionText(gen: number | null, product: string | null, project: string | null): string {
  return `g${dash(gen)} · ${dash(product)} / ${dash(project)}`;
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export function isLatest(site: FleetSite): boolean {
  const gen = site.current?.gen;
  return (
    site.connection === "online" &&
    site.desired !== null &&
    gen !== null &&
    gen !== undefined &&
    gen === site.desired.generation
  );
}

export function currentCell(site: FleetSite): CurrentCell {
  const current = site.current;
  if (current === null) return { kind: "none" };
  return {
    kind: site.connection === "online" ? "current" : "last-reported",
    version: versionText(current.gen, current.product, current.project_version),
    reportedAt: current.reported_at,
    source: current.source,
  };
}

/** 마지막 ApplyAttempt 요약: "succeeded · g2→g3 · 단계 verifying · 코드 E_CHECK". */
export function attemptText(site: FleetSite): string | null {
  const attempt = site.last_attempt;
  if (attempt === null) return null;
  const parts = [dash(attempt.result), `g${dash(attempt.from)}→g${dash(attempt.to)}`];
  if (attempt.phase !== null && attempt.phase !== attempt.result) parts.push(`단계 ${attempt.phase}`);
  if (attempt.code !== null) parts.push(`코드 ${attempt.code}`);
  return parts.join(" · ");
}

export function siteView(site: FleetSite): SiteView {
  const desired = site.desired;
  return {
    site: site.site,
    connection: site.connection,
    connectionLabel: CONNECTION_LABELS[site.connection],
    latest: isLatest(site),
    desired:
      desired === null
        ? LABELS.noDesired
        : versionText(desired.generation, desired.product_version, desired.project_version),
    current: currentCell(site),
    lastSeen: site.last_seen,
    attempt: attemptText(site),
    attemptSource: site.last_attempt === null ? null : sourceLabel(site.last_attempt.source),
    diskState: dash(site.disk_state),
  };
}

/** 시각을 "2026-11-01 18:00:00" 꼴로(브라우저 시간대, 시험은 timeZone을 넘긴다). */
export function formatTime(iso: string, timeZone?: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    ...(timeZone === undefined ? {} : { timeZone }),
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

/**
 * 표시 기준 시간대("GMT+9"). 표 머리·조회 시각 옆에 붙인다(시험은 timeZone을 넘긴다).
 * `at` 시점의 오프셋 하나다 — 일광 절약 시간 지역에서는 다른 계절의 시각과 오프셋이 다를 수 있다.
 */
export function timeZoneLabel(at: Date = new Date(), timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZoneName: "shortOffset",
    ...(timeZone === undefined ? {} : { timeZone }),
  }).formatToParts(at);
  return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
}

/** `/ui/fleet` 조회 실패. http = 응답은 왔지만 2xx가 아님, network = fetch 자체 실패. */
export type LoadFailure =
  | { kind: "http"; status: number }
  | { kind: "network"; detail: string }
  | { kind: "format" };

export function failureMessage(failure: LoadFailure): string {
  switch (failure.kind) {
    case "network":
      return `중앙 서버에 연결할 수 없습니다(${failure.detail}). 네트워크를 확인하고 다시 시도하세요.`;
    case "format":
      return "중앙 서버 응답 형식이 올바르지 않습니다.";
    case "http":
      switch (failure.status) {
        // 401은 Caddy basic auth(화면을 연 뒤 비밀번호 교체·만료)와 control-api(viewer 토큰 거부) 둘 다에서 온다.
        case 401:
          return "인증에 실패했습니다(HTTP 401) — 화면 로그인 또는 중앙 서버의 viewer 토큰. 다시 로그인하고, 계속되면 viewer 토큰 설정을 확인하세요.";
        case 403:
          return "조회 토큰에 fleet 조회 권한이 없습니다(HTTP 403). viewer 토큰인지 확인하세요.";
        case 503:
          return "control-api가 중앙 DB에 연결할 수 없습니다(HTTP 503). 잠시 후 다시 시도하세요.";
        case 502:
        case 504:
          return `control-api가 응답하지 않습니다(HTTP ${String(failure.status)}). 중앙 서버 상태를 확인하세요.`;
        default:
          return `현장 현황을 불러오지 못했습니다(HTTP ${String(failure.status)}).`;
      }
  }
}
