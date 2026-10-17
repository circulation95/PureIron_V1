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

/**
 * 전이 뒤 알람을 목록에 반영한다. NORMAL·RETIRED는 목록에서 뺀다. WS 이벤트는 한 연결 안에서 발행 순서대로 오므로
 * 시각 비교 없이 도착 순서대로 적용한다(서버 시계가 뒤로 가도 최신 전이를 버리지 않는다).
 */
export function upsertAlarm(alarms: readonly Alarm[], alarm: Alarm): Alarm[] {
  const rest = alarms.filter((a) => a.alarm_id !== alarm.alarm_id);
  if (alarm.state === "NORMAL" || alarm.state === "RETIRED") return rest;
  return sortAlarms([...rest, alarm]);
}

/** REST 결과(ACK 응답)는 목록에 이미 있는 알람만 갱신한다: 없다면 더 새로운 이벤트가 이미 뺀 것이다. */
export function applyRestAlarm(alarms: readonly Alarm[], alarm: Alarm): Alarm[] {
  return alarms.some((a) => a.alarm_id === alarm.alarm_id) ? upsertAlarm(alarms, alarm) : [...alarms];
}

/**
 * 스냅샷 위에 로드 중 버퍼된 이벤트를 도착 순서대로 재생한다. `changed_at` 비교는 여기서만 쓴다:
 * 같은 알람의 스냅샷이 더 새로우면 그 이벤트는 건너뛴다.
 */
export function mergeSnapshot(snapshot: readonly Alarm[], buffered: readonly Alarm[]): Alarm[] {
  const snapshotMs = new Map(snapshot.map((a) => [a.alarm_id, changedAtMs(a)]));
  return buffered
    .filter((event) => (snapshotMs.get(event.alarm_id) ?? 0) <= changedAtMs(event))
    .reduce(upsertAlarm, sortAlarms(snapshot));
}

export function isUnacked(alarm: Alarm): boolean {
  return alarm.state === "ACTIVE_UNACKED" || alarm.state === "CLEARED_UNACKED";
}
