// control-web e2e 스택(M4-3 G8). 준비·정리는 Python(`deploy/tests/central_stack.py`)이 맡는다 — 실제 Updater
// 코드로 enrollment를 만들고 현장 서명 heartbeat를 만드는 일은 Python 쪽에만 있다.
//
// 환경변수: PUREIRON_IT_PROJECT(프로젝트 `<값>-central`, 기본 pureiron-it)·PUREIRON_IT_CENTRAL_PORT(기본 28543)·
// PUREIRON_RELEASE(이미지 태그), PUREIRON_IT_KEEP=1(끝나도 내리지 않음).
// 상태 파일(basic auth 비밀번호·현장 토큰 포함)은 임시 디렉터리에 둔다 — artifacts/로 올라가지 않게.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const REPO = fileURLToPath(new URL("../../..", import.meta.url));
export const ARTIFACTS = join(REPO, "artifacts", "control-web-e2e");
const PORT = process.env.PUREIRON_IT_CENTRAL_PORT ?? "28543";
export const BASE_URL = `https://localhost:${PORT}`;
export const STATE_ENV = "PUREIRON_CONTROL_WEB_E2E_STATE"; // globalSetup → 테스트 worker

const COMMAND_TIMEOUT_MS = 10 * 60 * 1000;

export interface StackState {
  work: string; // 임시 작업 디렉터리(비밀 파일은 `<work>/secrets/`)
  base_url: string;
  user: string;
  password: string;
  site: string;
  site_token: string;
  heartbeat_file: string;
  reported_at: string;
  generation: number;
  poll_s: number;
}

export function runStack(args: readonly string[]): string {
  const result = spawnSync("uv", ["run", "python", "deploy/tests/central_stack.py", ...args], {
    cwd: REPO,
    encoding: "utf-8",
    timeout: COMMAND_TIMEOUT_MS,
  });
  const output = [result.error?.message, result.stdout, result.stderr].filter(Boolean).join("\n");
  if (result.status !== 0) throw new Error(`central_stack.py ${args.join(" ")} failed\n${output}`);
  return output;
}

export function readState(): StackState {
  const path = process.env[STATE_ENV];
  if (path === undefined) throw new Error(`${STATE_ENV} not set (globalSetup did not run)`);
  return JSON.parse(readFileSync(path, "utf-8")) as StackState;
}
