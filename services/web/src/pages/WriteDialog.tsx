// PLC 쓰기 확인 대화상자(plan M3c D8·M3c-3 E3~E5): 입력(현재값·범위·사유) → 검사(`POST /writes`, 아무것도
// 안 보냄) → 실행(`POST /writes/{id}/confirm`, 명령은 확인을 한 번 더) → 결과. 다시 보내기는 없다 —
// 새로 검사하면 서버가 그때의 현재값을 다시 보여준다.
//
// 보내는 동안에는 대화상자를 닫거나 화면을 떠날 수 없다(Esc·브라우저가 강제로 닫음·화면 이동·새로고침).
// 결과(특히 "결과 모름")를 못 본 운전자가 다시 쓰면 이중 쓰기가 된다(M3c-3 리뷰 HIGH-2·MEDIUM-1).
import { type SubmitEvent, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router";

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
import {
  checkLabel,
  confirmFailure,
  isWriteResult,
  type OutcomeView,
  outcomeView,
  UNKNOWN_RESULT,
} from "../app/writes";
import { formatTime, formatValue } from "../format";

const REASON_MAX_CHARS = 200; // core `WriteRequestIn.reason`과 같다
// 명령 2단계 확인 버튼을 잠깐 막는다: `실행` 더블클릭·Enter 연타가 확인 문구를 읽기 전에 들어가지 않게(리뷰 HIGH-1).
const COMMAND_ARM_MS = 1000;

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

interface Draft {
  text: string; // 설정값 입력
  command: number; // target.values 색인
  reason: string;
}

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

function draftValue(target: WriteTarget, draft: Draft): boolean | number | null {
  return target.kind === "setpoint" ? parseSetpoint(draft.text) : (target.values[draft.command] ?? null);
}

function TagValueText({ value }: { value: TagValue | null | undefined }) {
  if (value === null || value === undefined) return <>—</>;
  return (
    <>
      {formatValue(value.value)} <span className={`quality q-${value.quality.toLowerCase()}`}>{value.quality}</span>{" "}
      <span className="summary">{formatTime(value.ts)}</span>
    </>
  );
}

interface FactsProps {
  target: WriteTarget;
  current: TagValue | undefined; // 실시간 값
  snapshot: TagValue | null | undefined; // 검사(prepare) 시점 값, 검사 전이면 undefined
}

function Facts({ target, current, snapshot }: FactsProps) {
  return (
    <dl className="write-facts">
      <dt>장비</dt>
      <dd>{target.device_id}</dd>
      <dt>현재값</dt>
      <dd>
        <TagValueText value={current} />
      </dd>
      {snapshot === undefined ? null : (
        <>
          <dt>검사 시점 값</dt>
          <dd>
            <TagValueText value={snapshot} />
          </dd>
        </>
      )}
      {target.kind === "setpoint" ? (
        <>
          <dt>허용 범위</dt>
          <dd>
            {target.min === null ? "—" : formatValue(target.min)} ~ {target.max === null ? "—" : formatValue(target.max)}
          </dd>
        </>
      ) : (
        <>
          <dt>허용 값</dt>
          <dd>
            {target.values.length === 0
              ? "선언된 값이 없어 실행할 수 없습니다"
              : target.values.map(formatValue).join(", ")}
          </dd>
          <dt>허가 조건</dt>
          <dd>{target.permissive.join(", ")}</dd>
        </>
      )}
    </dl>
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

interface InputStepProps {
  target: WriteTarget;
  draft: Draft;
  setDraft: (draft: Draft) => void;
  checking: boolean;
  message: string | null;
  onCheck: (value: boolean | number) => void;
  onCancel: () => void;
}

function InputStep({ target, draft, setDraft, checking, message, onCheck, onCancel }: InputStepProps) {
  const value = draftValue(target, draft);
  const canCheck = value !== null && draft.reason.trim() !== "" && !checking;
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (value !== null && canCheck) onCheck(value);
  };
  return (
    <form className="write-form" onSubmit={submit}>
      {target.kind === "setpoint" ? (
        <label>
          목표값
          <input
            type="number"
            step="any"
            inputMode="decimal"
            value={draft.text}
            disabled={checking}
            onChange={(event) => {
              setDraft({ ...draft, text: event.target.value });
            }}
          />
        </label>
      ) : (
        <label>
          명령 값
          <select
            value={draft.command}
            disabled={checking}
            onChange={(event) => {
              setDraft({ ...draft, command: Number(event.target.value) });
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
          value={draft.reason}
          disabled={checking}
          onChange={(event) => {
            setDraft({ ...draft, reason: event.target.value });
          }}
        />
      </label>
      {message === null ? null : (
        <p className="error" role="alert">
          {message}
        </p>
      )}
      <div className="write-actions">
        <button type="button" onClick={onCancel} disabled={checking}>
          취소
        </button>
        <button type="submit" disabled={!canCheck}>
          {checking ? "검사 중…" : "검사"}
        </button>
      </div>
    </form>
  );
}

/** 명령 2단계 확인: 1단계 `실행` 자리에는 `취소`가 오고, `명령 실행`은 다른 자리에 잠깐 막힌 채 나온다. */
interface CommandConfirmProps {
  sending: boolean;
  onSend: () => void;
  onCancel: () => void;
}

function CommandConfirm({ sending, onSend, onCancel }: CommandConfirmProps) {
  const [armed, setArmed] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus(); // Enter 연타가 `취소`로 간다
    const timer = setTimeout(() => {
      setArmed(true);
    }, COMMAND_ARM_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);
  return (
    <div>
      <p className="notice" role="alert">
        운전 명령입니다. 장비가 바로 동작합니다. 정말 실행합니까?
      </p>
      <div className="write-actions">
        <button type="button" className="danger" disabled={!armed || sending} onClick={onSend}>
          {sending ? "실행 중…" : "명령 실행"}
        </button>
        <button type="button" ref={cancel} disabled={sending} onClick={onCancel}>
          취소
        </button>
      </div>
    </div>
  );
}

interface PreparedStepProps {
  target: WriteTarget;
  step: Extract<Step, { kind: "prepared" | "again" | "sending" }>;
  onAgain: () => void;
  onSend: (prepared: WritePrepared) => void;
  onCancel: () => void;
}

function PreparedStep({ target, step, onAgain, onSend, onCancel }: PreparedStepProps) {
  const sending = step.kind === "sending";
  // 명령은 2단계 확인을 거쳐야만 보낼 수 있으므로 보내는 중에도 그 화면을 유지한다.
  const confirming = target.kind === "command" && step.kind !== "prepared";
  const send = () => {
    onSend(step.prepared);
  };
  return (
    <>
      <p>
        목표값 <strong>{formatValue(step.prepared.value)}</strong> · 확인 기한 {formatTime(step.prepared.expires_at)}
      </p>
      <Checks checks={step.prepared.checks} />
      {confirming ? (
        <CommandConfirm sending={sending} onSend={send} onCancel={onCancel} />
      ) : (
        <div className="write-actions">
          <button type="button" onClick={onCancel} disabled={sending}>
            취소
          </button>
          <button type="button" disabled={sending} onClick={target.kind === "command" ? onAgain : send}>
            {sending ? "실행 중…" : "실행"}
          </button>
        </div>
      )}
    </>
  );
}

export function WriteDialog({ target, current, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const sentId = useRef<string | null>(null); // 같은 write_id를 두 번 보내지 않는다
  const closeRequested = useRef(false); // 사용자가 닫기를 골랐는지(아니면 브라우저가 닫아도 다시 연다)
  const [step, setStep] = useState<Step>({ kind: "input", message: null });
  const [draft, setDraft] = useState<Draft>({ text: "", command: 0, reason: "" });
  const sending = step.kind === "sending";
  const busy = step.kind === "checking" || sending;

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
    };
  }, []);

  // 보내는 중에는 화면 이동(뒤로 가기 포함)을 막고, 새로고침·탭 닫기에는 브라우저 경고를 띄운다.
  // 로그인 화면 이동(401)은 막지 않는다 — 막으면 세션 만료 뒤에도 이 화면에 갇힌다.
  const blocker = useBlocker(({ nextLocation }) => sending && nextLocation.pathname !== "/login");
  useEffect(() => {
    if (blocker.state === "blocked") blocker.reset();
  }, [blocker]);
  useEffect(() => {
    if (!sending) return undefined;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      window.removeEventListener("beforeunload", warn);
    };
  }, [sending]);

  const check = (value: boolean | number) => {
    setStep({ kind: "checking" });
    // prepare는 아무것도 보내지 않으므로 네트워크 오류도 "안 보냄"이다.
    api
      .POST("/api/v1/writes", { body: { tag_path: target.path, value, reason: draft.reason } })
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
    if (sentId.current === prepared.write_id) return;
    sentId.current = prepared.write_id;
    setStep({ kind: "sending", prepared });
    api
      .POST("/api/v1/writes/{write_id}/confirm", { params: { path: { write_id: prepared.write_id } } })
      .then(({ data, error, response }) => {
        if (response.ok) {
          setStep(
            isWriteResult(data) ? { kind: "done", result: data } : { kind: "outcome", view: UNKNOWN_RESULT },
          );
          return;
        }
        const checks = error !== undefined && "checks" in error ? error.checks : [];
        const failure = confirmFailure(response.status, error, checks);
        switch (failure.kind) {
          case "rejected":
            setStep({
              kind: "rejected",
              title: "보내기 직전 검사에서 거부되어 보내지 않았습니다.",
              checks: failure.checks,
            });
            return;
          case "expired":
            setStep({ kind: "input", message: "확인 시간이 지났습니다. 다시 검사하세요." });
            return;
          case "error":
            setStep({ kind: "error", message: failure.message });
            return;
          case "outcome":
            setStep({ kind: "outcome", view: failure.view });
        }
      })
      .catch(() => {
        setStep({ kind: "outcome", view: UNKNOWN_RESULT }); // 요청이 서버에 닿았는지 모른다
      });
  };

  const finish = () => {
    closeRequested.current = true;
    onClose();
  };
  const close = () => {
    if (!busy) finish();
  };

  return (
    <dialog
      ref={dialog}
      className="write-dialog"
      aria-labelledby="write-dialog-title"
      closedby={busy ? "none" : "closerequest"}
      onKeyDown={(event) => {
        if (event.key === "Escape" && busy) event.preventDefault(); // 반복 Esc도 막는다(cancel만으로는 못 막음)
      }}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClose={() => {
        const element = dialog.current;
        if (element === null || element.open) return; // StrictMode 재실행 등으로 다시 열린 뒤 도착한 close
        // 사용자가 고르지 않은 닫힘(반복 Esc에 브라우저가 강제로 닫음 등)이면 다시 연다 — 결과를 보여야 한다.
        if (!closeRequested.current) element.showModal();
      }}
    >
      <h2 id="write-dialog-title">
        {target.kind === "setpoint" ? "설정값 쓰기" : "운전 명령"} · {target.path}
      </h2>
      <Facts
        target={target}
        current={current}
        snapshot={step.kind === "prepared" || step.kind === "again" || sending ? step.prepared.current : undefined}
      />

      {step.kind === "input" || step.kind === "checking" ? (
        <InputStep
          target={target}
          draft={draft}
          setDraft={setDraft}
          checking={step.kind === "checking"}
          message={step.kind === "input" ? step.message : null}
          onCheck={check}
          onCancel={close}
        />
      ) : null}

      {step.kind === "rejected" ? (
        <>
          <p className="bad" role="alert">
            {step.title}
          </p>
          <Checks checks={step.checks.filter((c) => !c.ok)} />
          <div className="write-actions">
            <button type="button" onClick={finish}>
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
        <PreparedStep
          target={target}
          step={step}
          onAgain={() => {
            setStep({ kind: "again", prepared: step.prepared });
          }}
          onSend={send}
          onCancel={close}
        />
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
          <button type="button" onClick={finish}>
            닫기
          </button>
        </div>
      ) : null}
    </dialog>
  );
}
