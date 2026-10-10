import { defineConfig } from "vitest/config";

// The dev server proxies the Conversation API (`make serve` on :8000), so the browser talks to one
// origin and the backend needs no CORS. Everything binds to 127.0.0.1, like the rest of the stack.
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/v1": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
  },
});
