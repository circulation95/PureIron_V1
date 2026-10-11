// 알람 목록 갱신 규칙. 서버 `GET /alarms`와 같은 집합·순서를 WS `alarm_event`로 유지한다:
// NORMAL이 아닌 알람(활성 + 해제됐지만 미확인), 우선순위 높은 것 → 최근 전이 순.
import type { Alarm } from "../api/client";

const PRIORITY_RANK: Record<Alarm["priority"], number> = { critical: 3, high: 2, medium: 1, low: 0 };

function changedAtMs(alarm: Alarm): number {
  return alarm.changed_at === null ? 0 : Date.parse(alarm.changed_at);
}

export function sortAlarms(alarms: readonly Alarm[]): Alarm[] {
  return [...alarms].sort(
    (a, b) => PRIORITY_RANK[b.priority] - PRIORITY_RANK[a.priority] || changedAtMs(b) - changedAtMs(a),
  );
}

/** 전이 뒤 알람을 목록에 반영한다. NORMAL·RETIRED는 목록에서 뺀다. */
export function upsertAlarm(alarms: readonly Alarm[], alarm: Alarm): Alarm[] {
  const rest = alarms.filter((a) => a.alarm_id !== alarm.alarm_id);
  if (alarm.state === "NORMAL" || alarm.state === "RETIRED") return rest;
  return sortAlarms([...rest, alarm]);
}

export function isUnacked(alarm: Alarm): boolean {
  return alarm.state === "ACTIVE_UNACKED" || alarm.state === "CLEARED_UNACKED";
}
