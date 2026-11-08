// deploy/central 스택 기동·정리(M4-3 G8). 정리 때 compose 로그를 artifacts/control-web-e2e/compose.log로 남긴다.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { ARTIFACTS, runStack, STATE_ENV } from "./stack";

function saveLogs(state: string): void {
  try {
    writeFileSync(join(ARTIFACTS, "compose.log"), runStack(["logs", "--state", state]), "utf-8");
  } catch (err: unknown) {
    writeFileSync(join(ARTIFACTS, "compose.log"), `logs unavailable: ${String(err)}`, "utf-8");
  }
}

export default function globalSetup(): () => void {
  mkdirSync(ARTIFACTS, { recursive: true });
  const dir = mkdtempSync(join(tmpdir(), "pureiron-control-web-e2e-"));
  const state = join(dir, "stack.json");
  process.env[STATE_ENV] = state;
  const teardown = () => {
    saveLogs(state);
    try {
      runStack(["down", "--state", state]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };
  try {
    runStack(["up", "--out", state]);
  } catch (err: unknown) {
    teardown();
    throw err;
  }
  return teardown;
}
