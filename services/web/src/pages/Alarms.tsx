// 알람: 활성 목록(레이아웃이 WS `alarm_event`로 유지)과 ACK·전체 ACK(운전원 이상), 저널 탭.
import { useEffect, useState } from "react";

import {
  type Alarm,
  api,
  failureMessage,
  hasRole,
  type JournalEntry,
  type Load,
  networkMessage,
} from "../api/client";
import { isUnacked } from "../app/alarms";
import { useSession } from "../app/session";
import { formatTime, formatValue } from "../format";

const JOURNAL_LIMIT_ROWS = 200;

const STATE_LABELS: Record<Alarm["state"], string> = {
  NORMAL: "정상",
  ACTIVE_UNACKED: "활성·미확인",
  ACTIVE_ACKED: "활성·확인",
  CLEARED_UNACKED: "해제·미확인",
  RETIRED: "정의 삭제",
};

const PRIORITY_LABELS: Record<Alarm["priority"], string> = {
  critical: "긴급",
  high: "높음",
  medium: "보통",
  low: "낮음",
};

// 저널의 상태는 계약상 문자열이라 모르는 값은 그대로 보여준다.
const JOURNAL_STATE_LABELS: ReadonlyMap<string, string> = new Map(Object.entries(STATE_LABELS));

function stateLabel(state: string): string {
  return JOURNAL_STATE_LABELS.get(state) ?? state;
}

type Tab = "active" | "journal";

export function Alarms() {
  const [tab, setTab] = useState<Tab>("active");
  return (
    <section>
      <h1>알람</h1>
      <div className="tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "active"}
          onClick={() => {
            setTab("active");
          }}
        >
          활성
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "journal"}
          onClick={() => {
            setTab("journal");
          }}
        >
          저널
        </button>
      </div>
      {tab === "active" ? <ActiveAlarms /> : <Journal />}
    </section>
  );
}

function ActiveAlarms() {
  const { me, alarms, applyAlarms } = useSession();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const canAck = hasRole(me.role, "operator");

  const run = (request: Promise<{ changed: readonly Alarm[] } | { message: string }>) => {
    setBusy(true);
    setActionError(null);
    request
      .then((result) => {
        if ("changed" in result) applyAlarms(result.changed);
        else setActionError(result.message);
      })
      .catch((err: unknown) => {
        setActionError(networkMessage(err));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const ack = (alarm: Alarm) => {
    run(
      api
        .POST("/api/v1/alarms/{alarm_id}/ack", { params: { path: { alarm_id: alarm.alarm_id } } })
        .then(({ data, response }) =>
          data !== undefined
            ? { changed: [data] }
            : { message: response.status === 409 ? "이미 확인된 알람입니다." : failureMessage(response) },
        ),
    );
  };

  const ackAll = () => {
    run(
      api
        .POST("/api/v1/alarms/ack-all")
        .then(({ data, response }) =>
          data !== undefined ? { changed: data.acknowledged } : { message: failureMessage(response) },
        ),
    );
  };

  if (alarms.kind === "loading") return <p className="page-status">알람을 불러오는 중…</p>;
  if (alarms.kind === "error") return <p className="page-status error">{alarms.message}</p>;

  const unacked = alarms.value.filter(isUnacked).length;
  return (
    <>
      <div className="toolbar">
        <span className="summary">
          {alarms.value.length}개 · 미확인 {unacked}개
        </span>
        {canAck && (
          <button type="button" onClick={ackAll} disabled={busy || unacked === 0}>
            전체 ACK
          </button>
        )}
      </div>
      {actionError !== null && (
        <p className="error" role="alert">
          {actionError}
        </p>
      )}
      {alarms.value.length === 0 ? (
        <p>활성 알람이 없습니다.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>우선순위</th>
              <th>알람</th>
              <th>태그</th>
              <th>상태</th>
              <th className="num">값</th>
              <th className="num">설정값</th>
              <th>시각</th>
              {canAck && <th aria-label="확인" />}
            </tr>
          </thead>
          <tbody>
            {alarms.value.map((alarm) => (
              <tr key={alarm.alarm_id} data-alarm-id={alarm.alarm_id}>
                <td>
                  <span className={`priority p-${alarm.priority}`}>{PRIORITY_LABELS[alarm.priority]}</span>
                </td>
                <td>{alarm.description ?? alarm.alarm_id}</td>
                <td>{alarm.tag_path}</td>
                <td className={isUnacked(alarm) ? "alarm-state unacked" : "alarm-state"}>
                  {STATE_LABELS[alarm.state]}
                </td>
                <td className="num">{formatValue(alarm.value)}</td>
                <td className="num">
                  {alarm.alarm_type === "above" ? "> " : "< "}
                  {formatValue(alarm.setpoint)}
                </td>
                <td>{formatTime(alarm.changed_at)}</td>
                {canAck && (
                  <td>
                    {isUnacked(alarm) && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          ack(alarm);
                        }}
                      >
                        ACK
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Journal() {
  const [journal, setJournal] = useState<Load<JournalEntry[]>>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/alarms/journal", { params: { query: { limit: JOURNAL_LIMIT_ROWS } }, signal: controller.signal })
      .then(({ data, response }) => {
        setJournal(
          data === undefined
            ? { kind: "error", message: failureMessage(response) }
            : { kind: "ok", value: data.entries },
        );
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setJournal({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);

  if (journal.kind === "loading") return <p className="page-status">저널을 불러오는 중…</p>;
  if (journal.kind === "error") return <p className="page-status error">{journal.message}</p>;
  if (journal.value.length === 0) return <p>저널 기록이 없습니다.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>시각</th>
          <th>알람</th>
          <th>태그</th>
          <th>전이</th>
          <th className="num">값</th>
          <th>사용자</th>
        </tr>
      </thead>
      <tbody>
        {journal.value.map((entry) => (
          <tr key={entry.id}>
            <td>{formatTime(entry.time)}</td>
            <td>{entry.alarm_id}</td>
            <td>{entry.tag_path}</td>
            <td>
              {stateLabel(entry.from_state)} → {stateLabel(entry.to_state)}
            </td>
            <td className="num">{formatValue(entry.value)}</td>
            <td>{entry.username ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
