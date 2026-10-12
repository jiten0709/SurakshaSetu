import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Chat } from "./Chat";
import type { TurnResponse } from "./client";
import { CHOOSE, HANDOFF, S3_RENDER, SELECTION } from "./fixtures";
import { created, fakeApi, json, paragraph, turn } from "./test-support";

// The hand-off ending on bodies captured from the API: after HANDOFF the session is closed, the
// server's hand-off text is shown, and a new conversation is offered.

const replies = () => [...document.querySelectorAll<HTMLElement>("li.server")];

async function handedOff() {
  const api = fakeApi();
  api.answer(json(200, S3_RENDER));
  render(<Chat busyRetryMs={0} />);
  const steps: [string, TurnResponse][] = [
    ["Apply for one of these plans", CHOOSE],
    ["Apply for Suraksha Term Shield", SELECTION],
    ["I have read them", HANDOFF],
  ];
  for (const [label, next] of steps) {
    const button = await waitFor(() => {
      const b = within(replies().at(-1)!).getByRole("button", { name: label }) as HTMLButtonElement;
      if (b.disabled) throw new Error("not yet live");
      return b;
    });
    api.answer(json(200, next));
    fireEvent.click(button);
  }
  await screen.findByText(paragraph(HANDOFF.message.parts[0]!.text));
  return api;
}

describe("after HANDOFF", () => {
  it("shows the server's hand-off text and closes the conversation with a new-conversation control", async () => {
    await handedOff();
    expect(HANDOFF.state).toBe("HANDOFF");
    expect(screen.getByText("This conversation has ended.")).toBeTruthy();
    const restart = await screen.findByRole("button", { name: "New conversation" });
    await waitFor(() => expect(document.activeElement).toBe(restart));
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    const choices = within(screen.getByRole("log")).getAllByRole("button") as HTMLButtonElement[];
    expect(choices.every((b) => b.disabled)).toBe(true); // nothing earlier can be chosen again
    expect(screen.getByRole("button", { name: "Erase my data" })).toBeTruthy(); // the session is still held
  });

  it("starts a fresh session with START on New conversation", async () => {
    const api = await handedOff();
    api.answer(json(200, turn("S0", ["A fresh greeting"])));
    fireEvent.click(await screen.findByRole("button", { name: "New conversation" }));
    await screen.findByText("A fresh greeting");
    const fresh = created(2);
    const last = api.calls.at(-1);
    expect(last?.url).toBe(`/v1/sessions/${fresh.session_id}/turns`);
    expect(last?.headers.authorization).toBe(`Bearer ${fresh.session_token}`);
    expect(last?.body).toEqual({ action: { type: "START", payload: {} } });
    expect(screen.queryByText(paragraph(HANDOFF.message.parts[0]!.text))).toBeNull();
  });
});
