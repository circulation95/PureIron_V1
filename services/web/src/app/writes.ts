// PLC 쓰기 화면의 표시 규칙(plan M3c D10·M3c-3 E1·E4·E5): 버튼 표시 조건, 검사 이름, 결과 문구,
// confirm 실패 응답의 분류.
// 결과 판정은 core가 정본이고 여기는 그 결과를 운전자가 오해하지 않게 그리는 규칙만 둔다 —
// "결과 모름"(NO_RESPONSE)과 "값 미확인"(설정값 WRITTEN)을 성공으로 읽게 하지 않는다.
import {
  type CheckName,
  hasRole,
  type Role,
  type WriteCheck,
  type WriteResult,
  type WriteTarget,
  type WriteTargets,
} from "../api/client";
import { formatValue } from "../format";

/** 쓰기 버튼을 보일 태그(tag_id → 선언): 스위치가 켜져 있고 사용자 역할이 선언 role 이상인 것만. */
export function writableTargets(targets: WriteTargets, role: Role): ReadonlyMap<number, WriteTarget> {
  if (!targets.enabled) return new Map();
  return new Map(targets.targets.filter((t) => hasRole(role, t.role)).map((t) => [t.tag_id, t]));
}

export const CHECK_LABELS: Record<CheckName, string> = {
  writes_enabled: "현장 쓰기 스위치 켜짐",
  declared: "쓰기 선언된 태그",
  role: "역할 충족",
  value: "값이 허용 범위·형식 안",
  tag_good: "대상 태그 품질 GOOD",
  device_connected: "장비 연결됨",
  single_collector_session: "수집기 연결 1개",
  plan_reported: "수집기가 현재 정의로 수집 중",
  send_window: "응답 대기 시간 확보",
  permissive: "허가 조건 전부 참",
  no_edit_lock: "정의 편집 잠금 없음",
  not_in_flight: "같은 태그 진행 중 쓰기 없음",
};

// 계약에 새 검사가 생겨도 화면이 깨지지 않게 모르는 이름은 그대로 보여준다.
const CHECK_LABEL_MAP: ReadonlyMap<string, string> = new Map(Object.entries(CHECK_LABELS));

export function checkLabel(name: string): string {
  return CHECK_LABEL_MAP.get(name) ?? name;
}

export type Tone = "ok" | "warn" | "bad";

export interface OutcomeView {
  tone: Tone;
  title: string;
  detail: string;
}

/** 결과를 알 수 없을 때(collector 무응답·confirm 응답 유실)의 문구. 다시 누르면 이중 쓰기가 될 수 있다. */
export const UNKNOWN_RESULT: OutcomeView = {
  tone: "warn",
  title: "응답 없음 — 결과를 알 수 없습니다",
  detail: "값이 써졌을 수 있습니다. 현장에서 현재값을 확인하세요. 자동으로 다시 보내지 않습니다.",
};

export function outcomeView(result: WriteResult): OutcomeView {
  const readback = result.readback === null ? "—" : formatValue(result.readback);
  switch (result.outcome) {
    case "CONFIRMED":
      return { tone: "ok", title: "적용 확인", detail: `장비에서 다시 읽은 값 ${readback}이(가) 목표값과 같습니다.` };
    case "WRITTEN":
      // 설정값인데 다시 읽지 못했으면 장비가 받은 것만 안다(M3c-2 리뷰 LOW-3).
      return result.kind === "setpoint"
        ? {
            tone: "warn",
            title: "장비 수신, 값 미확인",
            detail: "장비가 쓰기를 받았지만 값을 다시 읽지 못했습니다. 현재값을 확인하세요.",
          }
        : { tone: "ok", title: "장비 수신", detail: "장비가 명령을 받았습니다." };
    case "MISMATCH":
      return { tone: "bad", title: "값 불일치", detail: `장비에서 다시 읽은 값(${readback})이 목표값과 다릅니다.` };
    case "FAILED":
      return { tone: "bad", title: "쓰지 못했습니다", detail: "장비가 쓰기를 거부했거나 연결되어 있지 않습니다." };
    case "REJECTED":
      return { tone: "bad", title: "보내지 않았습니다", detail: "보내기 직전 검사에서 거부되었습니다." };
    case "NO_RESPONSE":
    default: // 계약에 새 값이 생겨도 성공으로 읽지 않는다
      return UNKNOWN_RESULT;
  }
}

// core가 confirm 503에 싣는 detail(`api/writes.py` AUDIT_UNAVAILABLE, `api/errors.py` 세션 가드). 둘 다
// 보내기 전에만 난다. 그 밖의 503(앞단 프록시 등)은 보냈는지 모른다.
const NOT_SENT_503: ReadonlyMap<string, string> = new Map([
  ["audit unavailable; write not sent", "감사 기록에 실패해 장비로 보내지 않았습니다."],
  ["site-db unavailable", "현장 DB에 연결할 수 없어 장비로 보내지 않았습니다."],
]);

function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("detail" in body)) return null;
  return typeof body.detail === "string" ? body.detail : null;
}

/** 결과 응답 모양 확인 — 프록시·버전 불일치로 다른 본문이 와도 "결과 모름"으로 그린다. */
export function isWriteResult(body: unknown): body is WriteResult {
  return (
    typeof body === "object" &&
    body !== null &&
    "outcome" in body &&
    typeof body.outcome === "string" &&
    "kind" in body &&
    "value" in body &&
    (typeof body.value === "number" || typeof body.value === "boolean") &&
    "readback" in body &&
    (body.readback === null || typeof body.readback === "number") &&
    "before" in body &&
    (body.before === null || (typeof body.before === "object" && "value" in body.before))
  );
}

/** confirm이 결과 없이 끝났을 때 화면이 할 일(plan M3c-3 E5). */
export type ConfirmFailure =
  | { kind: "rejected"; checks: WriteCheck[] } // 보내기 직전 검사 거부(409) — 안 보냄
  | { kind: "expired" } // 410 — 안 보냄, 다시 검사
  | { kind: "error"; message: string } // 핸들러 전 거부 또는 이미 처리된 요청
  | { kind: "outcome"; view: OutcomeView };

export function confirmFailure(status: number, body: unknown, checks: WriteCheck[]): ConfirmFailure {
  switch (status) {
    case 409:
      return { kind: "rejected", checks };
    case 410:
      return { kind: "expired" };
    case 404:
      return {
        kind: "error",
        message:
          "이미 처리됐거나 없는 요청입니다. 현재값을 확인하고, 필요하면 엔지니어에게 감사 로그(tag.write) 확인을 요청하세요.",
      };
    case 401:
      return { kind: "error", message: "로그인이 필요합니다. 보내지 않았습니다." };
    case 403:
      return { kind: "error", message: "이 작업을 할 권한이 없습니다. 보내지 않았습니다." };
    case 422:
      return { kind: "error", message: "요청 값이 올바르지 않습니다. 보내지 않았습니다." };
    case 503: {
      const reason = NOT_SENT_503.get(detailOf(body) ?? "");
      if (reason !== undefined) return { kind: "outcome", view: { tone: "bad", title: "보내지 않았습니다", detail: reason } };
      return { kind: "outcome", view: UNKNOWN_RESULT };
    }
    default:
      // 그 밖의 실패(게이트웨이 시간 초과 등)는 서버가 보냈는지 모른다.
      return { kind: "outcome", view: UNKNOWN_RESULT };
  }
}
