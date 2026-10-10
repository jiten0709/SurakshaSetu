import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, vi, type MockInstance } from "vitest";

// Every test fails on a console call: the UI logs nothing, because a line could carry the session
// token or the customer's words. React's own warnings (act, keys) fail the test the same way.
let spies: [string, MockInstance][] = [];

beforeEach(() => {
  const methods = (Object.keys(console) as (keyof Console)[]).filter(
    (name) => typeof console[name] === "function",
  );
  spies = methods.map((name) => [name, vi.spyOn(console, name as "log").mockImplementation(() => {})]);
});

afterEach(() => {
  cleanup();
  const called = spies.filter(([, spy]) => spy.mock.calls.length > 0).map(([name]) => name);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  expect(called, "console.* was called").toEqual([]);
});
