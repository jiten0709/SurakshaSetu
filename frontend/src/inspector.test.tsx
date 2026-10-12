import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Chat } from "./Chat";
import { created, fakeApi, json, problem, turn } from "./test-support";

// The dev inspector (Step 30): with VITE_INSPECTOR=1 on the dev server only, the FSM state, the
// transition row and its reason per turn, read from the audit trail's headers. The browser sends no
// key (the dev-server proxy adds it), and nothing but STATE_TRANSITION headers is shown.

const AUDIT_URL = `/internal/audit/sessions/${created(1).session_id}/events`;

const transition = (seq: number, from: string, to: string, trigger: string, reason: string | null) => ({
  seq,
  event_type: "STATE_TRANSITION",
  occurred_at: "2026-10-12T10:00:00.000000+00:00",
  fsm_state: from,
  header: { from_state: from, to_state: to, trigger, invariants: { I1: true }, reason_code: reason },
});

/** What the endpoint answers, plus two sentinels the panel must never show. */
const EVENTS = [
  {
    seq: 1,
    event_type: "TURN_INPUT",
    occurred_at: "2026-10-12T10:00:00.000000+00:00",
    fsm_state: "S0",
    header: { turn_seq: 1, language: "en", channel: "web", note: "HEADER-SENTINEL" },
    payload_enc: "PAYLOAD-SENTINEL", // never sent by the API; the panel must not read it if it were
  },
  transition(2, "S0", "S0", "S0.STAY", null),
  transition(3, "S0", "S1", "S0.4", "NEW_PURCHASE"),
];

const panel = () => screen.queryByRole("complementary", { name: "Inspector" });

async function opened() {
  const api = fakeApi();
  api.answer(json(200, turn("S1", ["Hello from the greeting"])));
  render(<Chat busyRetryMs={0} />);
  await screen.findByText("Hello from the greeting");
  return api;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("the dev inspector", () => {
  it("is absent, and reads nothing, without the flag", async () => {
    const api = await opened();
    expect(panel()).toBeNull();
    expect(api.calls.filter((c) => c.url.startsWith("/internal/"))).toEqual([]);
  });

  it("shows the state, row and reason per turn from STATE_TRANSITION headers only, with no key sent", async () => {
    vi.stubEnv("VITE_INSPECTOR", "1");
    const api = fakeApi();
    api.audit(() => json(200, EVENTS));
    api.answer(json(200, turn("S1", ["Hello from the greeting"])));
    render(<Chat busyRetryMs={0} />);
    await screen.findByText("Hello from the greeting");
    const inspector = await screen.findByRole("complementary", { name: "Inspector" });
    await within(inspector).findByText("S1 · S0.4 · NEW_PURCHASE");
    const turns = within(inspector).getAllByRole("listitem").map((li) => li.textContent);
    expect(turns).toEqual(["1 · S0 → S0 · S0.STAY", "2 · S0 → S1 · S0.4 · NEW_PURCHASE"]);
    const reads = api.calls.filter((c) => c.url.startsWith("/internal/"));
    expect(reads.length).toBeGreaterThan(0);
    for (const read of reads) {
      expect(read.method).toBe("GET");
      expect(read.url).toBe(AUDIT_URL);
      expect(read.headers).not.toHaveProperty("authorization");
    }
    expect(document.body.textContent).not.toContain("PAYLOAD-SENTINEL");
    expect(document.body.textContent).not.toContain("HEADER-SENTINEL");
  });

  it("says 'not configured' when the proxy has no key, and the chat still works", async () => {
    vi.stubEnv("VITE_INSPECTOR", "1");
    const api = fakeApi();
    api.audit(() => problem(401, "UNAUTHORIZED"));
    api.answer(json(200, turn("S1", ["Hello from the greeting"])));
    render(<Chat busyRetryMs={0} />);
    await screen.findByText("Hello from the greeting");
    await waitFor(() => expect(panel()?.textContent).toContain("not configured"));
    api.answer(json(200, turn("S1", ["How old are you?"])));
    fireEvent.change(screen.getByLabelText("Your message"), { target: { value: "34" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("How old are you?")).toBeTruthy();
    expect(api.turns().at(-1)?.body).toEqual({ text: "34" });
    expect(panel()?.textContent).toContain("not configured");
  });
});
