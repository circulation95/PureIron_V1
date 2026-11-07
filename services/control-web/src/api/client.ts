// `/ui/fleet` 조회. 타입은 packages/contracts/openapi/control-api.json에서 생성한 schema.d.ts가 정본이다
// (`GET /api/v1/fleet`의 200 응답). 브라우저는 control-api를 직접 부르지 않고 토큰도 갖지 않는다(★G1):
// Caddy가 basic auth 뒤에서 이 경로 하나만 viewer 토큰을 붙여 control-api로 넘긴다(deploy/central/Caddyfile).
import type { FleetSite, LoadFailure } from "../fleetView";
import type { paths } from "./schema";

/** 정본 경로와 화면 경로의 대응(Caddy가 `/ui/fleet` → `/api/v1/fleet`로 바꾼다). */
type FleetResponse = paths["/api/v1/fleet"]["get"]["responses"][200]["content"]["application/json"];
export const FLEET_PATH = "/ui/fleet";

export type FleetLoad = { ok: true; sites: FleetResponse } | { ok: false; failure: LoadFailure };

function isFleet(body: unknown): body is FleetSite[] {
  return (
    Array.isArray(body) &&
    body.every((row: unknown) => typeof row === "object" && row !== null && "site" in row && "connection" in row)
  );
}

export async function fetchFleet(signal: AbortSignal): Promise<FleetLoad> {
  let response: Response;
  try {
    response = await fetch(FLEET_PATH, { headers: { Accept: "application/json" }, cache: "no-store", signal });
  } catch (err: unknown) {
    return { ok: false, failure: { kind: "network", detail: err instanceof Error ? err.message : String(err) } };
  }
  if (!response.ok) return { ok: false, failure: { kind: "http", status: response.status } };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false, failure: { kind: "format" } };
  }
  return isFleet(body) ? { ok: true, sites: body } : { ok: false, failure: { kind: "format" } };
}
