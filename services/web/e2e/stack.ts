// web-e2e용 compose 스택(plan M3b D14). core 통합 테스트(`services/core/tests/integration/conftest.py`)와
// 같은 방식이다: 프로젝트 `pureiron-it`(dev override) → site-db → `migrate up` → 전체 기동 → 시뮬레이터 100태그 +
// 알람 1개 정의 apply(호스트 `uv run pureiron-core`) → CLI로 테스트 계정 생성(기본 계정 없음, D3).
// M3c-3: core는 쓰기 스위치를 켠 채(`PUREIRON_WRITES_ENABLED=true`) 올리고, 시뮬레이터 메모리 영역에
// 쓰기 선언(설정값 1·명령 1·허가 조건 coil 1)을 둔다. 쓰기 검사가 통과할 때까지(수집·plan 보고) 기다린다.
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
export const USERS = { engineer: "e2e-engineer", operator: "e2e-operator", viewer: "e2e-viewer" } as const;

// 쓰기 선언(시뮬레이터 메모리 영역: holding 100.., coil 0..). 허가 조건 coil은 시험이 직접 바꾼다.
export const WRITES = {
  setpoint: "sim/sp_float", // holding 100-101 float32, 0~500
  command: "sim/start", // coil 10, 허용 값 [true], 허가 조건 REMOTE_OK
  permissive: "sim/remote_ok", // coil 20(기본 거짓)
} as const;
const SETPOINT_ADDRESS = 100;
const SETPOINT_MAX = 500;
const COMMAND_COIL = 10;
const PERMISSIVE_COIL = 20;
const SECRETS_DIR_ENV = "PUREIRON_E2E_SECRETS_DIR"; // globalSetup → 테스트 worker(compose exec용)

interface Stack {
  secretsDir: string;
  env: NodeJS.ProcessEnv;
}

function stackOf(secretsDir: string): Stack {
  return {
    secretsDir,
    env: {
      PUREIRON_SECRETS_DIR: secretsDir,
      PUREIRON_DB_DATA: "pureiron-db",
      PUREIRON_WEB_BIND: "127.0.0.1",
      PUREIRON_WEB_PORT: WEB_PORT,
      PUREIRON_WRITES_ENABLED: "true",
    },
  };
}

function run(cmd: string, args: readonly string[], env: NodeJS.ProcessEnv, stdin?: string) {
  const result = spawnSync(cmd, args, {
    cwd: REPO,
    env: { ...process.env, ...env },
    input: stdin,
    encoding: "utf-8",
    timeout: COMMAND_TIMEOUT_MS,
  });
  return { ok: result.status === 0, output: [result.error?.message, result.stdout, result.stderr].filter(Boolean).join("\n") };
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
  const coil = (path: string, address: number) => ({
    path,
    device: "sim",
    modbus: { register_type: "coil", address, data_type: "bool" },
  });
  write("tags/sim.yaml", [
    ...Array.from({ length: SIM_TAGS }, (_, i) => ({
      path: `sim/r${String(i).padStart(3, "0")}`,
      device: "sim",
      modbus: { register_type: "holding", address: i, data_type: "uint16" },
    })),
    {
      path: WRITES.setpoint,
      device: "sim",
      modbus: { register_type: "holding", address: SETPOINT_ADDRESS, data_type: "float32" },
      write: { kind: "setpoint", min: 0, max: SETPOINT_MAX },
    },
    {
      ...coil(WRITES.command, COMMAND_COIL),
      write: { kind: "command", values: [true], permissive: [WRITES.permissive] },
    },
    coil(WRITES.permissive, PERMISSIVE_COIL),
  ]);
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

async function login(username: string): Promise<string> {
  const response = await fetch(`${CORE_API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password: E2E_PASSWORD }),
    signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`login ${username} failed: ${String(response.status)}`);
  const body: unknown = await response.json();
  if (typeof body !== "object" || body === null || !("token" in body) || typeof body.token !== "string") {
    throw new Error(`login ${username}: no token in response`);
  }
  return body.token;
}

/** 수집·plan 보고가 끝나 유효한 설정값 쓰기가 검사를 통과할 때까지(prepare만 — 아무것도 안 보낸다). */
async function waitWritable(): Promise<void> {
  const token = await login(USERS.operator);
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let last = "";
  while (Date.now() < deadline) {
    const response = await fetch(`${CORE_API}/writes`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ tag_path: WRITES.setpoint, value: 1, reason: "e2e readiness" }),
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    if (response.ok) return;
    last = `${String(response.status)} ${await response.text()}`;
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error(`writes not ready within ${String(READY_TIMEOUT_MS)}ms (last: ${last})`);
}

/** 시뮬레이터 coil을 직접 바꾼다(허가 조건 시험). 테스트 worker에서 부른다. */
export function setSimCoil(address: number, value: boolean): void {
  const secretsDir = process.env[SECRETS_DIR_ENV];
  if (secretsDir === undefined) throw new Error(`${SECRETS_DIR_ENV} not set (globalSetup did not run)`);
  const script = [
    "from pymodbus.client import ModbusTcpClient",
    "c = ModbusTcpClient('127.0.0.1', port=5020)",
    "assert c.connect()",
    `r = c.write_coil(${String(address)}, ${value ? "True" : "False"}, device_id=1)`,
    "assert not r.isError(), r",
    "c.close()",
    "",
  ].join("\n");
  compose(stackOf(secretsDir), ["exec", "-T", "simulator", "python", "-"], { stdin: script });
}

export function setPermissive(value: boolean): void {
  setSimCoil(PERMISSIVE_COIL, value);
}

export async function startStack(): Promise<Stack> {
  const secretsDir = mkdtempSync(join(tmpdir(), "pureiron-e2e-"));
  const dbPassword = randomBytes(24).toString("base64url");
  writeFileSync(join(secretsDir, "db_password"), dbPassword, "utf-8");
  writeFileSync(join(secretsDir, "updater_token"), randomBytes(32).toString("base64url"), "utf-8");
  const stack = stackOf(secretsDir);
  process.env[SECRETS_DIR_ENV] = secretsDir;
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
    addUser(stack, USERS.operator, "operator");
    addUser(stack, USERS.viewer, "viewer");
    await waitWritable();
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
