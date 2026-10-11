// web-e2e용 compose 스택(plan M3b D14). core 통합 테스트(`services/core/tests/integration/conftest.py`)와
// 같은 방식이다: 프로젝트 `pureiron-it`(dev override) → site-db → `migrate up` → 전체 기동 → 시뮬레이터 100태그 +
// 알람 1개 정의 apply(호스트 `uv run pureiron-core`) → CLI로 테스트 계정 생성(기본 계정 없음, D3).
//
// 환경변수(conftest와 같은 이름): PUREIRON_IT_PROJECT·PUREIRON_DEV_DB_PORT·PUREIRON_DEV_CORE_PORT·
// PUREIRON_IT_WEB_PORT(기본 pureiron-it·25432·18000·18080), PUREIRON_IT_KEEP=1(끝나도 내리지 않음).
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const REPO = fileURLToPath(new URL("../../..", import.meta.url));
export const ARTIFACTS = join(REPO, "artifacts", "web-e2e");

const PROJECT = process.env.PUREIRON_IT_PROJECT ?? "pureiron-it";
const DB_PORT = process.env.PUREIRON_DEV_DB_PORT ?? "25432";
const CORE_PORT = process.env.PUREIRON_DEV_CORE_PORT ?? "18000";
export const WEB_PORT = process.env.PUREIRON_IT_WEB_PORT ?? "18080";
const CORE_API = `http://127.0.0.1:${CORE_PORT}/api/v1`;

const COMMAND_TIMEOUT_MS = 10 * 60 * 1000; // 이미지 pull 없이도 `up --wait`가 site-db health를 기다린다
const READY_TIMEOUT_MS = 120 * 1000;
const POLL_INTERVAL_MS = 1000;
const HTTP_TIMEOUT_MS = 3000;

export const SIM_TAGS = 100;
export const ALARM_ID = "sim-r000-high";
// 시뮬레이터 사인파 1000 + 500·sin(t/10 + address)의 중간값 → 주기(~63s)마다 조건이 성립·해제된다.
const ALARM_SETPOINT = 1000;
export const E2E_PASSWORD = "e2e-pass-1234";
export const USERS = { engineer: "e2e-engineer", viewer: "e2e-viewer" } as const;

interface Stack {
  secretsDir: string;
  env: NodeJS.ProcessEnv;
}

function run(cmd: string, args: readonly string[], env: NodeJS.ProcessEnv, stdin?: string) {
  const result = spawnSync(cmd, args, {
    cwd: REPO,
    env: { ...process.env, ...env },
    input: stdin,
    encoding: "utf-8",
    timeout: COMMAND_TIMEOUT_MS,
  });
  return { ok: result.status === 0, output: `${result.stdout}${result.stderr}` };
}

function compose(stack: Stack, args: readonly string[], options: { check?: boolean; stdin?: string } = {}) {
  const files = ["-f", "deploy/compose/site.compose.yml", "-f", "deploy/compose/site.dev.yml"];
  const result = run("docker", ["compose", "-p", PROJECT, ...files, "--profile", "dev", ...args], stack.env, options.stdin);
  if ((options.check ?? true) && !result.ok) {
    throw new Error(`docker compose ${args.join(" ")} failed\n${result.output}`);
  }
  return result;
}

async function waitHttp(url: string): Promise<void> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(HTTP_TIMEOUT_MS) });
      if (response.ok) return;
      last = `status ${String(response.status)}`;
    } catch (err: unknown) {
      last = err instanceof Error ? err.message : String(err);
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error(`${url} not ready within ${String(READY_TIMEOUT_MS)}ms (last: ${last})`);
}

function writeSimProject(root: string): void {
  const write = (path: string, value: unknown) => {
    writeFileSync(join(root, path), JSON.stringify(value), "utf-8"); // JSON은 YAML이기도 하다
  };
  for (const dir of ["devices", "tags", "alarms"]) mkdirSync(join(root, dir), { recursive: true });
  write("project.yaml", { schema_version: 1, project_id: "e2e", name: "web e2e" });
  write("devices/sim.yaml", { id: "sim", protocol: "modbus_tcp", host: "simulator", port: 5020 });
  write(
    "tags/sim.yaml",
    Array.from({ length: SIM_TAGS }, (_, i) => ({
      path: `sim/r${String(i).padStart(3, "0")}`,
      device: "sim",
      modbus: { register_type: "holding", address: i, data_type: "uint16" },
    })),
  );
  write("alarms/alarms.yaml", [
    { id: ALARM_ID, tag: "sim/r000", type: "above", setpoint: ALARM_SETPOINT, priority: "high" },
  ]);
}

function addUser(stack: Stack, username: string, role: string): void {
  const added = compose(stack, ["exec", "-T", "core", "pureiron-core", "users", "add", username, "--role", role], {
    check: false,
    stdin: `${E2E_PASSWORD}\n`,
  });
  if (!added.ok) throw new Error(`users add ${username} failed\n${added.output}`);
}

export async function startStack(): Promise<Stack> {
  const secretsDir = mkdtempSync(join(tmpdir(), "pureiron-e2e-"));
  const dbPassword = randomBytes(24).toString("base64url");
  writeFileSync(join(secretsDir, "db_password"), dbPassword, "utf-8");
  writeFileSync(join(secretsDir, "updater_token"), randomBytes(32).toString("base64url"), "utf-8");
  const stack: Stack = {
    secretsDir,
    env: {
      PUREIRON_SECRETS_DIR: secretsDir,
      PUREIRON_DB_DATA: "pureiron-db",
      PUREIRON_WEB_BIND: "127.0.0.1",
      PUREIRON_WEB_PORT: WEB_PORT,
    },
  };
  compose(stack, ["down", "-v"], { check: false });
  try {
    compose(stack, ["up", "-d", "--wait", "site-db"]);
    compose(stack, ["run", "--rm", "--no-deps", "core", "migrate", "up"]);
    compose(stack, ["up", "-d"]);
    await waitHttp(`${CORE_API}/readyz`);

    const projectDir = join(secretsDir, "sim-project");
    writeSimProject(projectDir);
    const applied = run("uv", ["run", "pureiron-core", "apply-definitions", projectDir, "--json"], {
      PUREIRON_DB_DSN: `postgresql://pureiron@127.0.0.1:${DB_PORT}/pureiron`,
      PUREIRON_DB_PASSWORD_FILE: join(secretsDir, "db_password"),
      PUREIRON_MIGRATIONS_DIR: join(REPO, "services", "core", "migrations"),
      PYTHONUTF8: "1",
    });
    if (!applied.ok) throw new Error(`apply-definitions failed\n${applied.output}`);

    addUser(stack, USERS.engineer, "engineer");
    addUser(stack, USERS.viewer, "viewer");
    await waitHttp(`http://127.0.0.1:${WEB_PORT}/`);
  } catch (err: unknown) {
    stopStack(stack);
    throw err;
  }
  return stack;
}

export function stopStack(stack: Stack): void {
  if (process.env.PUREIRON_IT_KEEP === "1") return;
  const logs = compose(stack, ["logs", "--no-color", "--tail", "200"], { check: false });
  mkdirSync(ARTIFACTS, { recursive: true });
  writeFileSync(join(ARTIFACTS, "compose.log"), logs.output, "utf-8");
  compose(stack, ["down", "-v"], { check: false });
  rmSync(stack.secretsDir, { recursive: true, force: true });
}
