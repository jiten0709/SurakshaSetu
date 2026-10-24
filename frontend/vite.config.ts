import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// The dev server proxies the Conversation API (`make serve` on :8000), so the browser talks to one
// origin and the backend needs no CORS. Everything binds to 127.0.0.1, like the rest of the stack.
export default defineConfig(({ mode }) => {
  // Step 30: the dev inspector's one read (the audit headers of a session) gets the compliance key
  // here, in the dev server, from a gitignored .env.local or the environment; it never reaches the
  // browser or a build. Without it the API answers 401 and the panel says "not configured".
  const key = loadEnv(mode, process.cwd(), "SS_COMPLIANCE_API_KEY").SS_COMPLIANCE_API_KEY;
  // In the compose `ui` service the API is the `orchestrator` service (UI_API_TARGET); on the host
  // it is `make serve` on localhost.
  const api = process.env.UI_API_TARGET ?? "http://127.0.0.1:8000";
  return {
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/v1": api,
        "^/internal/audit/sessions/[0-9a-f-]+/events$": {
          target: api,
          headers: key ? { Authorization: `Bearer ${key}` } : {},
        },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["src/test-setup.ts"],
      css: true, // otherwise a stylesheet (even ?raw) reads as "" and the font-stack test sees nothing
      // Vite reads .env.local in test mode too: a developer's VITE_INSPECTOR=1 must not change the
      // tests (each inspector test sets the flag itself).
      env: { VITE_INSPECTOR: "" },
    },
  };
});
