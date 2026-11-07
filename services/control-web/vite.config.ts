// control-web 개발 서버 — 개발 전용. 운영·e2e에서는 Caddy(deploy/central)가 정적 파일과 `/ui/fleet`을 맡는다.
// basic auth가 없고 기본 대상 127.0.0.1:8100은 M4-2 리허설 control-api다(PUREIRON_CONTROL_URL로 바꾼다).
//
// 브라우저는 토큰을 갖지 않는다(M4-3 ★G1): `/ui/fleet`만 control-api `GET /api/v1/fleet`로 넘기고,
// viewer 토큰은 개발 서버가 파일(PUREIRON_CONTROL_VIEWER_TOKEN_FILE)에서 읽어 붙인다 — Caddy와 같은 모양.
import { readFileSync } from "node:fs";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const controlUrl = process.env.PUREIRON_CONTROL_URL ?? "http://127.0.0.1:8100";
const tokenFile = process.env.PUREIRON_CONTROL_VIEWER_TOKEN_FILE;

function viewerToken(): string | undefined {
  return tokenFile === undefined ? undefined : readFileSync(tokenFile, "utf-8").trim();
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3100,
    proxy: {
      "^/ui/fleet$": {
        target: controlUrl,
        rewrite: () => "/api/v1/fleet",
        configure: (proxy) => {
          proxy.on("proxyReq", (request, incoming) => {
            // Caddy와 같이 GET에만 토큰을 붙인다(다른 메서드는 토큰 없이 넘어가 control-api가 거절).
            const token = viewerToken();
            if (token !== undefined && incoming.method === "GET") {
              request.setHeader("Authorization", `Bearer ${token}`);
            }
          });
        },
      },
    },
  },
  build: { sourcemap: false },
});
