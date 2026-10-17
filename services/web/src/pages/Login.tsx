// 로그인 화면. 성공하면 원래 가려던 경로(`?next=`)로 돌아간다. 실패 문구는 고정이다(서버 문구를 보여주지 않음).
import { type SubmitEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { api } from "../api/client";
import { isLoginPath } from "../app/Layout";

type Submit = { kind: "idle" } | { kind: "sending" } | { kind: "error"; message: string };

/** 같은 origin의 경로만 돌아갈 곳으로 받는다(`//evil.example` 같은 외부 이동 차단). `/login`으로는 돌아가지 않는다. */
function safeNext(next: string | null): string {
  if (next === null) return "/";
  try {
    const url = new URL(next, window.location.origin);
    if (url.origin !== window.location.origin || isLoginPath(url.pathname)) return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

export function Login() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submit, setSubmit] = useState<Submit>({ kind: "idle" });

  const onSubmit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmit({ kind: "sending" });
    api
      .POST("/api/v1/auth/login", { body: { username, password } })
      .then(({ data, response }) => {
        if (data !== undefined) {
          void navigate(safeNext(params.get("next")), { replace: true });
          return;
        }
        setSubmit({
          kind: "error",
          message:
            response.status === 401 || response.status === 422
              ? "아이디 또는 비밀번호가 올바르지 않습니다."
              : "지금은 로그인할 수 없습니다. 잠시 후 다시 시도하세요.",
        });
      })
      .catch(() => {
        setSubmit({ kind: "error", message: "core에 연결할 수 없습니다. 잠시 후 다시 시도하세요." });
      });
  };

  return (
    <main className="login">
      <form onSubmit={onSubmit} aria-labelledby="login-title">
        <h1 id="login-title">PureIron 로그인</h1>
        <label>
          아이디
          <input
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
            }}
          />
        </label>
        <label>
          비밀번호
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
        </label>
        {submit.kind === "error" && (
          <p className="error" role="alert">
            {submit.message}
          </p>
        )}
        <button type="submit" disabled={submit.kind === "sending"}>
          {submit.kind === "sending" ? "확인 중…" : "로그인"}
        </button>
      </form>
    </main>
  );
}
