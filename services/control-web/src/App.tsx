// 현장 현황 화면 1장(읽기 전용, M4-3 G7): 30초마다 자동 새로고침 + 수동 새로고침.
// 조회가 실패하면 표를 지우고 오류만 보인다 — 이전 표를 남기면 끊긴 현장이 "연결됨"으로 남아 보인다.
import { useEffect, useState } from "react";

import { fetchFleet } from "./api/client";
import { FleetTable } from "./FleetTable";
import { failureMessage, formatTime, siteView, timeZoneLabel, type SiteView } from "./fleetView";

export const REFRESH_MS = 30_000;

type State =
  | { kind: "loading" }
  | { kind: "ok"; rows: SiteView[]; fetchedAt: string }
  | { kind: "error"; message: string; failedAt: string };

export function App() {
  // 조회 차례 번호. 자동(30초)·수동 새로고침 모두 이것을 올리고, 조회는 차례마다 한 번(앞 차례는 취소).
  const [round, setRound] = useState(0);
  const [settledRound, setSettledRound] = useState(-1);
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const timer = window.setInterval(() => {
      setRound((r) => r + 1);
    }, REFRESH_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchFleet(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      const now = new Date().toISOString();
      setState(
        result.ok
          ? { kind: "ok", rows: result.sites.map(siteView), fetchedAt: now }
          : { kind: "error", message: failureMessage(result.failure), failedAt: now },
      );
      setSettledRound(round);
    });
    return () => {
      controller.abort();
    };
  }, [round]);

  const busy = settledRound !== round;
  return (
    <main>
      <h1>현장 현황</h1>
      <div className="toolbar">
        <button
          type="button"
          onClick={() => {
            setRound((r) => r + 1);
          }}
          disabled={busy}
        >
          새로고침
        </button>
        <span className="summary">30초마다 자동으로 새로고침합니다. 읽기 전용 화면입니다.</span>
        {state.kind === "ok" && (
          <span className="summary" data-testid="fetched-at">
            조회 시각 {formatTime(state.fetchedAt)} ({timeZoneLabel()}, 표시 기준 시간대)
          </span>
        )}
      </div>
      {state.kind === "loading" && <p className="page-status">불러오는 중…</p>}
      {state.kind === "error" && (
        <p className="page-status error" role="alert">
          {state.message} <span className="summary">({formatTime(state.failedAt)})</span>
        </p>
      )}
      {state.kind === "ok" && <FleetTable rows={state.rows} />}
    </main>
  );
}
