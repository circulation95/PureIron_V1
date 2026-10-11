import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// dev: `docker compose ... -f site.dev.yml`가 core를 127.0.0.1:18000에 노출한다.
const coreUrl = process.env.PUREIRON_CORE_URL ?? "http://127.0.0.1:18000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // changeOrigin을 켜지 않는다: Host를 보존해야 core WS의 Origin 검사(host:port 비교)를 통과한다.
    proxy: { "/api": { target: coreUrl, ws: true } },
  },
  build: { sourcemap: false },
});
