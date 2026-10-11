// core API 클라이언트. 타입은 packages/contracts/openapi/core.json에서 생성한 schema.d.ts가 정본이다.
// 인증은 세션 쿠키(`pureiron_session`, HttpOnly·SameSite=Strict)라 모든 요청에 `credentials: "include"`.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type Me = Schemas["MeOut"];
export type Role = Schemas["RoleName"];
export type TagValue = Schemas["TagValueOut"];
export type Alarm = Schemas["AlarmOut"];
export type JournalEntry = Schemas["JournalEntryOut"];
export type Device = Schemas["DeviceOut"];
export type HistorySeries = Schemas["HistorySeries"];
export type AuditEntry = Schemas["AuditEntryOut"];

/** 비동기 화면 상태(coding-style 10절). */
export type Load<T> = { kind: "loading" } | { kind: "ok"; value: T } | { kind: "error"; message: string };

export const api = createClient<paths>({ baseUrl: "", credentials: "include" });

// 세션이 없거나 만료되면(401) 레이아웃이 로그인 화면으로 보낸다. 로그인 요청 자체의 401은 실패 메시지다.
export const authEvents = new EventTarget();
export const UNAUTHORIZED_EVENT = "unauthorized";

export function notifyUnauthorized(): void {
  authEvents.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

api.use({
  onResponse({ schemaPath, response }) {
    if (response.status === 401 && schemaPath !== "/api/v1/auth/login") {
      notifyUnauthorized();
    }
    return undefined;
  },
});

const ROLE_LEVEL: Record<Role, number> = { viewer: 0, operator: 1, engineer: 2, admin: 3 };

export const ROLE_LABELS: Record<Role, string> = {
  viewer: "조회자",
  operator: "운전원",
  engineer: "엔지니어",
  admin: "관리자",
};

export function hasRole(role: Role, minimum: Role): boolean {
  return ROLE_LEVEL[role] >= ROLE_LEVEL[minimum];
}

/** 실패 응답을 화면 문구로 바꾼다. 서버 문구는 그대로 보여주지 않는다. */
export function failureMessage(response: Response): string {
  switch (response.status) {
    case 401:
      return "로그인이 필요합니다.";
    case 403:
      return "이 작업을 할 권한이 없습니다.";
    case 422:
      return "요청 값이 올바르지 않습니다.";
    case 503:
      return "현장 DB에 연결할 수 없습니다. 잠시 후 다시 시도하세요.";
    default:
      return `요청을 처리하지 못했습니다(HTTP ${String(response.status)}).`;
  }
}

/** fetch 자체가 실패(네트워크·중단)했을 때의 화면 문구. */
export function networkMessage(err: unknown): string {
  return err instanceof Error ? `core에 연결할 수 없습니다: ${err.message}` : "core에 연결할 수 없습니다.";
}
