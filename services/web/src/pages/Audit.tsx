// 감사 로그: 사용자·행위·기간 필터 표(최신순). core가 엔지니어 이상만 허용하므로 그 아래는 안내만 보여준다.
import { type SubmitEvent, useEffect, useState } from "react";

import { api, type AuditEntry, failureMessage, hasRole, type Load, networkMessage } from "../api/client";
import { useSession } from "../app/session";
import { formatTime } from "../format";

const AUDIT_LIMIT_ROWS = 200;

interface Filter {
  username: string;
  action: string;
  start: string; // datetime-local 입력값(브라우저 로캘 시각), 빈 문자열 = 제한 없음
  end: string;
}

const EMPTY_FILTER: Filter = { username: "", action: "", start: "", end: "" };

function toIso(local: string): string | null {
  return local === "" ? null : new Date(local).toISOString();
}

function orNull(text: string): string | null {
  const trimmed = text.trim();
  return trimmed === "" ? null : trimmed;
}

export function Audit() {
  const { me } = useSession();
  return (
    <section>
      <h1>감사 로그</h1>
      {hasRole(me.role, "engineer") ? (
        <AuditTable />
      ) : (
        <p className="notice" role="note">
          권한이 없습니다. 감사 로그는 엔지니어 이상만 볼 수 있습니다.
        </p>
      )}
    </section>
  );
}

function AuditTable() {
  const [draft, setDraft] = useState<Filter>(EMPTY_FILTER);
  const [filter, setFilter] = useState<Filter>(EMPTY_FILTER);
  const [entries, setEntries] = useState<Load<AuditEntry[]>>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/audit", {
        params: {
          query: {
            username: orNull(filter.username),
            action: orNull(filter.action),
            start: toIso(filter.start),
            end: toIso(filter.end),
            limit: AUDIT_LIMIT_ROWS,
          },
        },
        signal: controller.signal,
      })
      .then(({ data, response }) => {
        setEntries(
          data === undefined
            ? { kind: "error", message: failureMessage(response) }
            : { kind: "ok", value: data.entries },
        );
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setEntries({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, [filter]);

  const onSubmit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setEntries({ kind: "loading" });
    setFilter({ ...draft });
  };

  const field = (key: keyof Filter) => ({
    value: draft[key],
    onChange: (event: { target: { value: string } }) => {
      setDraft((prev) => ({ ...prev, [key]: event.target.value }));
    },
  });

  return (
    <>
      <form className="filters" onSubmit={onSubmit}>
        <label>
          사용자
          <input name="username" {...field("username")} />
        </label>
        <label>
          행위
          <input name="action" placeholder="예: alarm.ack" {...field("action")} />
        </label>
        <label>
          시작
          <input name="start" type="datetime-local" {...field("start")} />
        </label>
        <label>
          끝
          <input name="end" type="datetime-local" {...field("end")} />
        </label>
        <button type="submit">조회</button>
      </form>
      {entries.kind === "loading" && <p className="page-status">감사 로그를 불러오는 중…</p>}
      {entries.kind === "error" && <p className="page-status error">{entries.message}</p>}
      {entries.kind === "ok" &&
        (entries.value.length === 0 ? (
          <p>조건에 맞는 기록이 없습니다.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>시각</th>
                <th>사용자</th>
                <th>행위</th>
                <th>대상</th>
                <th>경로</th>
                <th>상세</th>
              </tr>
            </thead>
            <tbody>
              {entries.value.map((entry) => (
                <tr key={entry.id}>
                  <td>{formatTime(entry.time)}</td>
                  <td>{entry.username ?? "—"}</td>
                  <td>{entry.action}</td>
                  <td>{entry.target ?? "—"}</td>
                  <td>{entry.source}</td>
                  <td className="detail">
                    {Object.keys(entry.detail).length === 0 ? "—" : JSON.stringify(entry.detail)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </>
  );
}
