// @vitest-environment node
import { build } from "vite";
import { afterEach, expect, it, vi } from "vitest";

// The dev inspector (Step 30) never ships: a production build made with the flag on and a compliance
// key in the environment carries neither the inspector nor the key. The positive control keeps the
// search honest (a build that emitted nothing would pass it otherwise).

const KEY = "key-SENTINEL-30";

afterEach(() => {
  vi.unstubAllEnvs();
});

it("the production build has no inspector and no compliance key", async () => {
  // As `npm run build` runs: vitest's NODE_ENV=test would make a development build (DEV true).
  vi.stubEnv("NODE_ENV", "production");
  vi.stubEnv("VITE_INSPECTOR", "1");
  vi.stubEnv("SS_COMPLIANCE_API_KEY", KEY);
  const result = await build({ logLevel: "silent", build: { write: false } });
  const outputs = (Array.isArray(result) ? result : [result]) as { output: { type: string; code?: string; source?: unknown }[] }[];
  const shipped = outputs
    .flatMap((o) => o.output)
    .map((file) => (file.type === "chunk" ? file.code : String(file.source)))
    .join("\n");
  // Booleans, so a failure names the word and never prints the bundle.
  expect(shipped.includes("Erase my data"), "positive control").toBe(true);
  for (const banned of ["/internal/", "Inspector", "not configured", "SS_COMPLIANCE_API_KEY", KEY]) {
    expect(shipped.includes(banned), banned).toBe(false);
  }
}, 60_000);
