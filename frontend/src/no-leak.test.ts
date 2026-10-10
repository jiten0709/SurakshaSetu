import { fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { expect, it, vi } from "vitest";
import { Chat } from "./Chat";
import { created, fakeApi, json, problem, turn } from "./test-support";

// The session token and the conversation live in memory only: never in storage, a cookie, the URL,
// the history or a log. The console guard (test-setup.ts) fails any test that calls console.*.

const TEXTS = ["Greeting SENTINEL-G", "Reply SENTINEL-R", "Closing SENTINEL-C", "Fresh SENTINEL-F"];
const TYPED = "my own words SENTINEL-T";

function everywhere(): string {
  const stored = (s: Storage) => Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) ?? "")}`);
  return [
    ...stored(localStorage),
    ...stored(sessionStorage),
    document.cookie,
    location.href,
    JSON.stringify(history.state),
    document.title,
  ].join("\n");
}

it("a whole conversation leaves no token or message text outside memory, and logs nothing", async () => {
  const api = fakeApi();
  const choice = { label: "Choice SENTINEL-Q", action: { type: "CONTINUE", payload: {} } } as const;
  api.answer(
    json(200, turn("S0", [TEXTS[0] ?? ""], [choice])),
    problem(503, "SERVICE_UNAVAILABLE"),
    json(200, turn("S1", [TEXTS[1] ?? ""], [choice])),
    json(200, turn("EXIT", [TEXTS[2] ?? ""])),
    json(200, turn("S0", [TEXTS[3] ?? ""])),
  );
  render(createElement(Chat, { busyRetryMs: 0 }));
  await screen.findByText(TEXTS[0] ?? "");
  fireEvent.change(screen.getByLabelText("Your message"), { target: { value: TYPED } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  await screen.findByText(TEXTS[1] ?? "");
  api.emit("turn.status", { turn_id: "t", status: "analysing" });
  fireEvent.click(screen.getAllByRole("button", { name: choice.label }).at(-1) as HTMLElement);
  await screen.findByText(TEXTS[2] ?? "");
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  await screen.findByText(TEXTS[3] ?? "");

  const secrets = [created(1).session_token, created(2).session_token, TYPED, choice.label, ...TEXTS];
  const outside = everywhere();
  expect(secrets.filter((s) => outside.includes(s))).toEqual([]);
  const urls = api.calls.map((c) => c.url).join("\n");
  expect(secrets.filter((s) => urls.includes(s))).toEqual([]);
});

it("the console guard sees a planted call", () => {
  console.warn("planted");
  expect(vi.mocked(console.warn)).toHaveBeenCalledTimes(1);
  vi.mocked(console.warn).mockClear();
});
