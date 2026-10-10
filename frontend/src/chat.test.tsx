import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Chat } from "./Chat";
import type { components } from "./conversation-api";
import { created, deferred, fakeApi, json, problem, turn } from "./test-support";

type QuickReply = components["schemas"]["QuickReply"];

const START = { action: { type: "START", payload: {} } };
const ACK: QuickReply = {
  label: "I have read them",
  action: {
    type: "DISCLOSURE_ACK",
    payload: {
      uin: "999N001V02",
      registry_version: "2026.09.1",
      disclosure_set_sha256: "a".repeat(64),
      document_sha256: { CIS: "b".repeat(64), POLICY_WORDING: "c".repeat(64) },
    },
  },
};
const HELP: QuickReply = { label: "Talk to a person", action: { type: "HUMAN_REQUEST", payload: {} } };

/** Opens the chat on a greeting with the given quick replies; returns the fake API. */
async function opened(state: components["schemas"]["FsmState"] = "S0", replies: QuickReply[] = []) {
  const api = fakeApi();
  api.answer(json(200, turn(state, ["Hello from the greeting", "The second part"], replies)));
  render(<Chat busyRetryMs={0} />);
  await screen.findByText("Hello from the greeting");
  return api;
}

function type(text: string) {
  fireEvent.change(screen.getByLabelText("Your message"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("opening", () => {
  it("creates a web session, sends START, and shows the reply's parts in order", async () => {
    const api = await opened();
    expect(api.calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "POST /v1/sessions",
      `GET ${created(1).events_url}`,
      `POST /v1/sessions/${created(1).session_id}/turns`,
    ]);
    expect(api.calls[0]?.body).toEqual({ channel: "web", locale: "en-IN" });
    expect(api.turns()[0]?.body).toEqual(START);
    const log = screen.getByRole("log");
    expect(log.getAttribute("aria-live")).toBe("polite");
    const texts = within(log).getAllByText(/./, { selector: "p" }).map((p) => p.textContent);
    expect(texts).toEqual(["Hello from the greeting", "The second part"]);
  });

  it("shows the whole text when a reply has no parts (a blocked release)", async () => {
    const api = fakeApi();
    const blocked = turn("S0", []);
    blocked.message.text = "Blocked release text";
    api.answer(json(200, blocked));
    render(<Chat busyRetryMs={0} />);
    expect(await screen.findByText("Blocked release text")).toBeTruthy();
  });

  it("offers Retry when the session cannot be created, and Retry creates one", async () => {
    const api = fakeApi();
    api.onCreate(problem(503, "SERVICE_UNAVAILABLE"));
    api.answer(json(200, turn("S0", ["Hello"])));
    render(<Chat busyRetryMs={0} />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/couldn't start a conversation/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Hello")).toBeTruthy();
    expect(api.turns()[0]?.headers.authorization).toBe(`Bearer ${created(1).session_token}`);
  });
});

describe("turns", () => {
  it("sends typed text with a fresh key per turn and echoes it", async () => {
    const api = await opened();
    api.answer(json(200, turn("S0", ["First answer"])), json(200, turn("S0", ["Second answer"])));
    type("I agree");
    await screen.findByText("First answer");
    type("yes");
    await screen.findByText("Second answer");
    const [start, one, two] = api.turns();
    expect([one?.body, two?.body]).toEqual([{ text: "I agree" }, { text: "yes" }]);
    const keys = new Set([start, one, two].map((c) => c?.headers["idempotency-key"]));
    expect(keys.size).toBe(3);
    expect(screen.getByText("I agree")).toBeTruthy();
  });

  it("a quick reply sends its action unchanged, once, and the buttons wait for the turn", async () => {
    const api = await opened("S3", [ACK, HELP]);
    const answer = deferred();
    api.answer(answer.promise);
    const ack = screen.getByRole("button", { name: ACK.label });
    act(() => {
      ack.click();
      ack.click();
    });
    expect(api.turns()).toHaveLength(2);
    expect(api.turns()[1]?.body).toEqual({ action: ACK.action });
    expect((screen.getByRole("button", { name: HELP.label }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    answer.resolve(json(200, turn("S3", ["Thanks"], [HELP])));
    await screen.findByText("Thanks");
    const helps = screen.getAllByRole("button", { name: HELP.label }) as HTMLButtonElement[];
    expect(helps.map((b) => b.disabled)).toEqual([true, false]);
  });

  it("the status line follows turn.status while a turn runs", async () => {
    const api = await opened();
    const answer = deferred();
    api.answer(answer.promise);
    type("hello");
    api.emit("turn.status", { turn_id: "t", status: "composing" });
    await screen.findByText("Writing a reply…");
    expect(screen.getByRole("status").textContent).toBe("Writing a reply…");
    answer.resolve(json(200, turn("S0", ["Done"])));
    await screen.findByText("Done");
    expect(screen.getByRole("status").textContent).toBe("");
  });
});

describe("problems", () => {
  it("RATE_LIMITED asks the customer to wait", async () => {
    const api = await opened();
    api.answer(problem(429, "RATE_LIMITED"));
    type("hello");
    expect((await screen.findByRole("alert")).textContent).toMatch(/wait a minute/);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("SESSION_BUSY is retried once with the same key, then explained", async () => {
    const api = await opened();
    api.answer(problem(409, "SESSION_BUSY"), problem(409, "SESSION_BUSY"));
    type("hello");
    expect((await screen.findByRole("alert")).textContent).toMatch(/still being answered/);
    const [, one, two] = api.turns();
    expect(api.turns()).toHaveLength(3);
    expect(two?.headers["idempotency-key"]).toBe(one?.headers["idempotency-key"]);
  });

  it("SESSION_BUSY then success shows the reply and no alert", async () => {
    const api = await opened();
    api.answer(problem(409, "SESSION_BUSY"), json(200, turn("S0", ["Got it"])));
    type("hello");
    await screen.findByText("Got it");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["SERVICE_UNAVAILABLE", problem(503, "SERVICE_UNAVAILABLE")],
    ["an unreachable service", new TypeError("Failed to fetch")],
    ["the dev proxy's empty 500", new Response("", { status: 500 })],
  ])("%s offers Retry, which resends the same body with the same key", async (_, failure) => {
    const api = await opened();
    api.answer(failure, json(200, turn("S0", ["Recovered"])));
    type("my answer");
    expect((await screen.findByRole("alert")).textContent).toMatch(/Nothing was lost/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Recovered");
    const [, failed, retried] = api.turns();
    expect(retried?.body).toEqual(failed?.body);
    expect(retried?.headers["idempotency-key"]).toBe(failed?.headers["idempotency-key"]);
    expect(screen.getAllByText("my answer")).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("SESSION_EXPIRED offers a new conversation, which starts afresh", async () => {
    const api = await opened();
    api.answer(problem(410, "SESSION_EXPIRED"), json(200, turn("S0", ["A fresh greeting"])));
    type("hello");
    expect((await screen.findByRole("alert")).textContent).toMatch(/expired/);
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    await screen.findByText("A fresh greeting");
    expect(screen.queryByText("Hello from the greeting")).toBeNull();
    const last = api.turns().at(-1);
    expect(last?.url).toBe(`/v1/sessions/${created(2).session_id}/turns`);
    expect(last?.headers.authorization).toBe(`Bearer ${created(2).session_token}`);
    expect(last?.body).toEqual(START);
  });

  it("a malformed body shows a generic error and offers a new conversation", async () => {
    const api = await opened();
    api.answer(json(200, { unexpected: true }));
    type("hello");
    expect((await screen.findByRole("alert")).textContent).toMatch(/Something went wrong/);
    expect(screen.getByRole("button", { name: "New conversation" })).toBeTruthy();
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
  });
});

describe("layout (found in the browser pass)", () => {
  it("the status line, a notice and the input share the sticky dock, so a notice is never hidden", async () => {
    const api = await opened();
    api.answer(problem(503, "SERVICE_UNAVAILABLE"));
    type("hello");
    const dock = (await screen.findByRole("alert")).closest(".dock");
    expect(dock?.contains(screen.getByRole("status"))).toBe(true);
    expect(dock?.contains(screen.getByLabelText("Your message"))).toBe(true);
  });

  it("scrolls the newest bubble in from its start on each reply and each notice", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      const api = await opened();
      expect(scroll).toHaveBeenLastCalledWith({ block: "start" });
      const before = scroll.mock.calls.length;
      api.answer(problem(503, "SERVICE_UNAVAILABLE"));
      type("hello");
      await screen.findByRole("alert");
      expect(scroll.mock.calls.length).toBeGreaterThan(before + 1); // the echo, then the notice
    } finally {
      delete (Element.prototype as Partial<Element>).scrollIntoView;
    }
  });
});

describe("closed sessions", () => {
  it("a terminal state disables typing, keeps the server's choices and offers a new conversation", async () => {
    const consent: QuickReply = { label: "Yes, an advisor may call", action: { type: "ADVISOR_CONTACT", payload: { granted: true } } };
    const api = await opened("HUMAN_ESCALATION", [consent]);
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("This conversation has ended.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "New conversation" })).toBeTruthy();
    api.answer(json(200, turn("HUMAN_ESCALATION", ["An advisor will call"])));
    fireEvent.click(screen.getByRole("button", { name: consent.label }));
    await screen.findByText("An advisor will call");
    expect(api.turns().at(-1)?.body).toEqual({ action: consent.action });
  });
});
