import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// dev: `docker compose ... -f site.dev.yml`가 core를 127.0.0.1:18000에 노출한다.
const coreUrl = process.env.PUREIRON_CORE_URL ?? "http://127.0.0.1:18000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: { "/api": { target: coreUrl, ws: true } },
  },
  build: { sourcemap: false },
});
