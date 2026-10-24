// PLC 쓰기 확인 대화상자(plan M3c D8·M3c-3 E3~E5): 입력(현재값·범위·사유) → 검사(`POST /writes`, 아무것도
// 안 보냄) → 실행(`POST /writes/{id}/confirm`, 명령은 확인을 한 번 더) → 결과. 다시 보내기는 없다 —
// 새로 검사하면 서버가 그때의 현재값을 다시 보여준다.
import { type SubmitEvent, useEffect, useRef, useState } from "react";

import {
  api,
  failureMessage,
  networkMessage,
  type TagValue,
  type WriteCheck,
  type WritePrepared,
  type WriteResult,
  type WriteTarget,
} from "../api/client";
import { checkLabel, type OutcomeView, outcomeView, UNKNOWN_RESULT } from "../app/writes";
import { formatTime, formatValue } from "../format";

const REASON_MAX_CHARS = 200; // core `WriteRequestIn.reason`과 같다

type Step =
  | { kind: "input"; message: string | null }
  | { kind: "checking" }
  | { kind: "rejected"; title: string; checks: WriteCheck[] }
  | { kind: "prepared"; prepared: WritePrepared }
  | { kind: "again"; prepared: WritePrepared } // 명령: 확인 한 번 더
  | { kind: "sending"; prepared: WritePrepared }
  | { kind: "done"; result: WriteResult }
  | { kind: "outcome"; view: OutcomeView } // confirm이 결과 없이 끝남(모름·보내지 않음)
  | { kind: "error"; message: string };

interface Props {
  target: WriteTarget;
  current: TagValue | undefined;
  onClose: () => void;
}

function parseSetpoint(text: string): number | null {
  if (text.trim() === "") return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function CurrentValue({ value }: { value: TagValue | null | undefined }) {
  if (value === null || value === undefined) return <dd>—</dd>;
  return (
    <dd>
      {formatValue(value.value)} <span className={`quality q-${value.quality.toLowerCase()}`}>{value.quality}</span>{" "}
      <span className="summary">{formatTime(value.ts)}</span>
    </dd>
  );
}

function Checks({ checks }: { checks: readonly WriteCheck[] }) {
  return (
    <ul className="checks" aria-label="실행 전 검사">
      {checks.map((check) => (
        <li key={check.name} data-check={check.name} className={check.ok ? "ok" : "bad"}>
          {check.ok ? "✓" : "✗"} {checkLabel(check.name)}
          {check.detail === null ? null : <span className="summary"> — {check.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

function Outcome({ view }: { view: OutcomeView }) {
  return (
    <div className={`outcome tone-${view.tone}`} role={view.tone === "ok" ? "status" : "alert"}>
      <strong>{view.title}</strong>
      <p>{view.detail}</p>
    </div>
  );
}

export function WriteDialog({ target, current, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState<Step>({ kind: "input", message: null });
  const [text, setText] = useState("");
  const [command, setCommand] = useState(0); // target.values 색인
  const [reason, setReason] = useState("");
  const busy = step.kind === "checking" || step.kind === "sending";

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
    };
  }, []);

  const isSetpoint = target.kind === "setpoint";
  const value: boolean | number | null = isSetpoint ? parseSetpoint(text) : (target.values[command] ?? null);
  const canCheck = value !== null && reason.trim() !== "" && !busy;

  const check = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (value === null || !canCheck) return;
    setStep({ kind: "checking" });
    // prepare는 아무것도 보내지 않으므로 네트워크 오류도 "안 보냄"이다.
    api
      .POST("/api/v1/writes", { body: { tag_path: target.path, value, reason } })
      .then(({ data, error, response }) => {
        if (data !== undefined) setStep({ kind: "prepared", prepared: data });
        else if (response.status === 409 && error !== undefined && "checks" in error)
          setStep({ kind: "rejected", title: "검사를 통과하지 못해 보내지 않았습니다.", checks: error.checks });
        else setStep({ kind: "input", message: failureMessage(response) });
      })
      .catch((err: unknown) => {
        setStep({ kind: "input", message: networkMessage(err) });
      });
  };

  const send = (prepared: WritePrepared) => {
    setStep({ kind: "sending", prepared });
    api
      .POST("/api/v1/writes/{write_id}/confirm", { params: { path: { write_id: prepared.write_id } } })
      .then(({ data, error, response }) => {
        if (data !== undefined) {
          setStep({ kind: "done", result: data });
          return;
        }
        switch (response.status) {
          case 409:
            setStep({
              kind: "rejected",
              title: "보내기 직전 검사에서 거부되어 보내지 않았습니다.",
              checks: error !== undefined && "checks" in error ? error.checks : [],
            });
            return;
          case 410:
            setStep({ kind: "input", message: "확인 시간이 지났습니다. 다시 검사하세요." });
            return;
          case 404:
            setStep({
              kind: "error",
              message: "이미 처리됐거나 없는 요청입니다. 결과는 현재값과 감사 로그로 확인하세요.",
            });
            return;
          case 503:
            setStep({
              kind: "outcome",
              view: { tone: "bad", title: "보내지 않았습니다", detail: "감사 기록에 실패해 장비로 보내지 않았습니다." },
            });
            return;
          case 401:
          case 403:
          case 422:
            setStep({ kind: "error", message: failureMessage(response) });
            return;
          default:
            // 그 밖의 실패(게이트웨이 시간 초과 등)는 서버가 보냈는지 모른다.
            setStep({ kind: "outcome", view: UNKNOWN_RESULT });
        }
      })
      .catch(() => {
        setStep({ kind: "outcome", view: UNKNOWN_RESULT }); // 요청이 서버에 닿았는지 모른다
      });
  };

  const close = () => {
    if (!busy) onClose();
  };

  return (
    <dialog
      ref={dialog}
      className="write-dialog"
      aria-labelledby="write-dialog-title"
      onCancel={(event) => {
        event.preventDefault(); // Esc: 진행 중이면 닫지 않는다
        close();
      }}
    >
      <h2 id="write-dialog-title">
        {isSetpoint ? "설정값 쓰기" : "운전 명령"} · {target.path}
      </h2>
      <dl className="write-facts">
        <dt>장비</dt>
        <dd>{target.device_id}</dd>
        <dt>현재값</dt>
        <CurrentValue value={step.kind === "prepared" || step.kind === "again" ? step.prepared.current : current} />
        {isSetpoint ? (
          <>
            <dt>허용 범위</dt>
            <dd>
              {target.min === null ? "—" : formatValue(target.min)} ~ {target.max === null ? "—" : formatValue(target.max)}
            </dd>
          </>
        ) : (
          <>
            <dt>허용 값</dt>
            <dd>{target.values.map(formatValue).join(", ")}</dd>
            <dt>허가 조건</dt>
            <dd>{target.permissive.join(", ")}</dd>
          </>
        )}
      </dl>

      {step.kind === "input" || step.kind === "checking" ? (
        <form className="write-form" onSubmit={check}>
          {isSetpoint ? (
            <label>
              목표값
              <input
                type="number"
                step="any"
                inputMode="decimal"
                value={text}
                disabled={busy}
                onChange={(event) => {
                  setText(event.target.value);
                }}
              />
            </label>
          ) : (
            <label>
              명령 값
              <select
                value={command}
                disabled={busy}
                onChange={(event) => {
                  setCommand(Number(event.target.value));
                }}
              >
                {target.values.map((v, i) => (
                  <option key={String(v)} value={i}>
                    {formatValue(v)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            사유
            <input
              type="text"
              maxLength={REASON_MAX_CHARS}
              value={reason}
              disabled={busy}
              onChange={(event) => {
                setReason(event.target.value);
              }}
            />
          </label>
          {step.kind === "input" && step.message !== null ? (
            <p className="error" role="alert">
              {step.message}
            </p>
          ) : null}
          <div className="write-actions">
            <button type="button" onClick={close} disabled={busy}>
              취소
            </button>
            <button type="submit" disabled={!canCheck}>
              {busy ? "검사 중…" : "검사"}
            </button>
          </div>
        </form>
      ) : null}

      {step.kind === "rejected" ? (
        <>
          <p className="bad" role="alert">
            {step.title}
          </p>
          <Checks checks={step.checks.filter((c) => !c.ok)} />
          <div className="write-actions">
            <button type="button" onClick={onClose}>
              닫기
            </button>
            <button
              type="button"
              onClick={() => {
                setStep({ kind: "input", message: null });
              }}
            >
              다시 입력
            </button>
          </div>
        </>
      ) : null}

      {step.kind === "prepared" || step.kind === "again" || step.kind === "sending" ? (
        <>
          <p>
            목표값 <strong>{formatValue(step.prepared.value)}</strong> · 확인 기한 {formatTime(step.prepared.expires_at)}
          </p>
          <Checks checks={step.prepared.checks} />
          {step.kind === "again" ? (
            <p className="notice" role="alert">
              운전 명령입니다. 장비가 바로 동작합니다. 정말 실행합니까?
            </p>
          ) : null}
          <div className="write-actions">
            <button type="button" onClick={close} disabled={busy}>
              취소
            </button>
            {step.kind === "prepared" && !isSetpoint ? (
              <button
                type="button"
                onClick={() => {
                  setStep({ kind: "again", prepared: step.prepared });
                }}
              >
                실행
              </button>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  send(step.prepared);
                }}
              >
                {busy ? "실행 중…" : step.kind === "again" ? "명령 실행" : "실행"}
              </button>
            )}
          </div>
        </>
      ) : null}

      {step.kind === "done" ? (
        <>
          <p>
            목표값 <strong>{formatValue(step.result.value)}</strong>
            {step.result.before === null ? null : <> · 보내기 직전 값 {formatValue(step.result.before.value)}</>}
          </p>
          <Outcome view={outcomeView(step.result)} />
        </>
      ) : null}
      {step.kind === "outcome" ? <Outcome view={step.view} /> : null}
      {step.kind === "error" ? (
        <p className="error" role="alert">
          {step.message}
        </p>
      ) : null}
      {step.kind === "done" || step.kind === "outcome" || step.kind === "error" ? (
        <div className="write-actions">
          <button type="button" onClick={onClose}>
            닫기
          </button>
        </div>
      ) : null}
    </dialog>
  );
}
