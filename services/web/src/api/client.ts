// core API 클라이언트. 타입은 packages/contracts/openapi/core.json에서 생성한 schema.d.ts가 정본이다.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Readiness = components["schemas"]["Readiness"];

export const api = createClient<paths>({ baseUrl: "" });

export async function fetchReadiness(signal: AbortSignal): Promise<Readiness> {
  // readyz는 미준비일 때 503과 같은 본문(Readiness)을 돌려준다.
  const { data, error } = await api.GET("/api/v1/readyz", { signal });
  return data ?? error;
}
