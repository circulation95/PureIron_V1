// M1 골격 화면: 현장 준비 상태(readyz). 로그인 + 5개 운영 화면은 M3b 범위(D9).
import { useEffect, useState } from "react";

import { fetchReadiness, type Readiness } from "./api/client";

const POLL_MS = 5000;

type Load = { kind: "loading" } | { kind: "ok"; value: Readiness } | { kind: "error"; message: string };

const CHECK_LABELS: Record<keyof Readiness["checks"], string> = {
  db: "현장 DB",
  schema_envelope: "스키마 버전",
  collector: "수집기 연결",
};

function CheckRow({ label, ok, detail }: { label: string; ok: boolean; detail: string | null | undefined }) {
  return (
    <li className={ok ? "check ok" : "check bad"}>
      <span className="mark" aria-hidden="true">
        {ok ? "●" : "○"}
      </span>
      <span className="label">{label}</span>
      <span className="detail">{ok ? "정상" : (detail ?? "확인 필요")}</span>
    </li>
  );
}

export function App() {
  const [state, setState] = useState<Load>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    const load = () => {
      fetchReadiness(controller.signal)
        .then((value) => {
          setState({ kind: "ok", value });
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted) {
            setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
          }
        });
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  return (
    <main>
      <h1>PureIron 현장 상태</h1>
      {state.kind === "loading" && <p>확인 중…</p>}
      {state.kind === "error" && <p className="error">core에 연결할 수 없습니다: {state.message}</p>}
      {state.kind === "ok" && (
        <>
          <p className={state.value.status === "ready" ? "status ok" : "status bad"}>
            {state.value.status === "ready" ? "운영 준비됨" : "준비되지 않음"} · core {state.value.version}
          </p>
          <ul className="checks">
            {(Object.keys(CHECK_LABELS) as (keyof Readiness["checks"])[]).map((key) => (
              <CheckRow
                key={key}
                label={CHECK_LABELS[key]}
                ok={state.value.checks[key].ok}
                detail={state.value.checks[key].detail}
              />
            ))}
          </ul>
        </>
      )}
    </main>
  );
}
