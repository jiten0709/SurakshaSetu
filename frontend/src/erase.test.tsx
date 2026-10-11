import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Chat } from "./Chat";
import { DECLINED, ERASED, GREETING_EN, INTENT_ASK } from "./fixtures";
import { created, deferred, fakeApi, json, paragraph, problem } from "./test-support";

// "Erase my data": a confirmation, then DELETE /v1/sessions/{id} (the ERASE turn), the server's own
// confirmation, and a clean start on request. The session's token is dropped once it is erased.

const SESSION_1 = `/v1/sessions/${created(1).session_id}`;
const erased = () => screen.findByText(paragraph(ERASED.message.parts[0]?.text ?? ""));

async function greeted(...more: (Response | Error | Promise<Response>)[]) {
  const api = fakeApi();
  api.answer(json(200, GREETING_EN), ...more);
  render(<Chat busyRetryMs={0} />);
  await screen.findByRole("form", { name: "Consent form" });
  return api;
}

function confirmErase() {
  fireEvent.click(screen.getByRole("button", { name: "Erase my data" }));
  fireEvent.click(screen.getByRole("button", { name: "Erase" }));
}

const deletes = (api: ReturnType<typeof fakeApi>) => api.calls.filter((c) => c.method === "DELETE");

describe("erase", () => {
  it("asks first; Cancel sends nothing and gives focus back", async () => {
    const api = await greeted();
    fireEvent.click(screen.getByRole("button", { name: "Erase my data" }));
    expect(screen.getByText("Erase your data and end this conversation?")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Erase your data and end this conversation?")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Erase my data" }));
    expect(deletes(api)).toEqual([]);
  });

  it("DELETEs the session, shows the server's confirmation, then starts afresh only on request", async () => {
    const api = await greeted(json(200, ERASED), json(200, GREETING_EN));
    confirmErase();
    await erased();
    const [call] = deletes(api);
    expect(call?.url).toBe(SESSION_1);
    expect(call?.headers.authorization).toBe(`Bearer ${created(1).session_token}`);
    expect(call?.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Erase my data" })).toBeNull();
    const before = api.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    await screen.findByRole("form", { name: "Consent form" });
    expect(screen.queryByText(paragraph(ERASED.message.parts[0]?.text ?? ""))).toBeNull();
    const after = api.calls.slice(before);
    expect(after.map((c) => `${c.method} ${c.url}`)).toEqual([
      "POST /v1/sessions",
      `GET ${created(2).events_url}`,
      `POST /v1/sessions/${created(2).session_id}/turns`,
    ]);
    expect(after.some((c) => c.headers.authorization?.includes(created(1).session_token))).toBe(false);
  });

  it("while a turn is in flight, waits for it, then erases (nothing else in between)", async () => {
    const answer = deferred();
    const api = await greeted(answer.promise, json(200, ERASED));
    fireEvent.click(screen.getByLabelText(GREETING_EN.message.form.adult.label));
    fireEvent.click(screen.getByRole("button", { name: GREETING_EN.message.form.submit }));
    confirmErase();
    expect(deletes(api)).toEqual([]);
    answer.resolve(json(200, INTENT_ASK));
    await erased();
    expect(api.calls.slice(-2).map((c) => c.method)).toEqual(["POST", "DELETE"]);
    const replies = [...document.querySelectorAll("li.server")].map((li) => li.textContent ?? "");
    expect(replies).toHaveLength(3);
    expect(replies[1]).toContain(INTENT_ASK.message.parts[0]?.text);
    expect(replies[2]).toContain(ERASED.message.parts[0]?.text);
  });

  it("a repeat on a gone session (401) is a plain result, not an error", async () => {
    const api = await greeted(new TypeError("Failed to fetch"), problem(401, "UNAUTHORIZED"));
    confirmErase();
    expect((await screen.findByRole("alert")).textContent).toMatch(/Nothing was lost/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" })); // the first DELETE did land
    const plain = await screen.findByText("This conversation has already been erased.");
    expect(plain.closest('[role="status"]')).not.toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "New conversation" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Erase my data" })).toBeNull();
    const [first, again] = deletes(api);
    expect(again?.headers["idempotency-key"]).toBe(first?.headers["idempotency-key"]);
  });

  it("is offered after the conversation has closed (I5: from any state), and the session is then dropped", async () => {
    // From a closed state the erasure's reply keeps that state (EXIT.STAY; seen in the browser pass):
    // a 200 from DELETE is what says the session is gone.
    const erasedInExit = { ...ERASED, state: "EXIT" as const };
    const api = await greeted(json(200, DECLINED), json(200, erasedInExit));
    fireEvent.click(screen.getByRole("button", { name: GREETING_EN.message.form.submit }));
    await screen.findByText(paragraph(DECLINED.message.parts[0]?.text ?? ""));
    confirmErase();
    await erased();
    expect(deletes(api).map((c) => c.url)).toEqual([SESSION_1]);
    expect(screen.queryByRole("button", { name: "Erase my data" })).toBeNull();
  });

  it("lives in the sticky dock with the language switch, so it stays visible (found in the browser pass)", async () => {
    await greeted();
    const dock = screen.getByLabelText("Your message").closest(".dock");
    expect(dock?.contains(screen.getByRole("button", { name: "Erase my data" }))).toBe(true);
    expect(dock?.contains(screen.getByRole("button", { name: "हिंदी" }))).toBe(true);
  });

  it("opening the confirmation scrolls the newest bubble back above the taller dock (browser pass)", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      await greeted();
      const before = scroll.mock.calls.length;
      fireEvent.click(screen.getByRole("button", { name: "Erase my data" }));
      expect(scroll.mock.calls.length).toBe(before + 1);
      expect(scroll).toHaveBeenLastCalledWith({ block: "start" });
    } finally {
      delete (Element.prototype as Partial<Element>).scrollIntoView;
    }
  });

  it("is not offered before a session exists", async () => {
    const api = fakeApi();
    api.onCreate(problem(503, "SERVICE_UNAVAILABLE"));
    render(<Chat busyRetryMs={0} />);
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "Erase my data" })).toBeNull();
  });
});
