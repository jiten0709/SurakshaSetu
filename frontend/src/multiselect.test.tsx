import { fireEvent, render, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Chat } from "./Chat";
import type { QuickReply } from "./client";
import { fakeApi, json, turn } from "./test-support";

// S2's goals: choices the server marks `multiple` are toggles that send nothing; Continue sends one
// SLOT action listing the picks in the order picked (the server ranks them by it). Any other quick
// reply beside them still sends its own action at once.

const goal = (value: string, label: string): QuickReply => ({
  label,
  action: { type: "SLOT", payload: { slot: "goals", value } },
  multiple: true,
});
const GOALS = turn(
  "S2",
  ["Which goals?"],
  [
    goal("income_protection", "Protect your family's income"),
    goal("loan_cover", "Repay loans"),
    goal("child_education", "Fund a child's education"),
    { label: "Talk to an advisor", action: { type: "HUMAN_REQUEST", payload: {} } },
  ],
);
const NEXT = turn("S2", ["Next question"]);

const replies = () => [...document.querySelectorAll<HTMLElement>("li.server")];
const button = (reply: HTMLElement, name: string) =>
  within(reply).getByRole("button", { name }) as HTMLButtonElement;
const pressed = (b: HTMLElement) => b.getAttribute("aria-pressed");
const sent = (api: ReturnType<typeof fakeApi>) => api.turns().map((c) => c.body);

async function atGoals() {
  const api = fakeApi();
  api.answer(json(200, GOALS));
  render(<Chat busyRetryMs={0} />);
  await waitFor(() => expect(replies()).toHaveLength(1));
  await waitFor(() => expect(button(replies()[0]!, "Repay loans").disabled).toBe(false));
  return { api, reply: replies()[0]! };
}

describe("choices that may be combined", () => {
  it("toggle without sending; Continue appears with the first pick and goes with the last", async () => {
    const { api, reply } = await atGoals();
    expect(within(reply).queryByRole("button", { name: "Continue" })).toBeNull();
    fireEvent.click(button(reply, "Repay loans"));
    expect(pressed(button(reply, "Repay loans"))).toBe("true");
    expect(pressed(button(reply, "Protect your family's income"))).toBe("false");
    expect(sent(api)).toHaveLength(1); // START only
    expect(button(reply, "Continue")).toBeTruthy();
    fireEvent.click(button(reply, "Repay loans"));
    expect(pressed(button(reply, "Repay loans"))).toBe("false");
    expect(within(reply).queryByRole("button", { name: "Continue" })).toBeNull();
  });

  it("Continue sends one SLOT with the picks in the order picked, echoed as their labels", async () => {
    const { api, reply } = await atGoals();
    fireEvent.click(button(reply, "Fund a child's education"));
    fireEvent.click(button(reply, "Protect your family's income"));
    api.answer(json(200, NEXT));
    fireEvent.click(button(reply, "Continue"));
    await waitFor(() => expect(replies()).toHaveLength(2));
    expect(sent(api).at(-1)).toEqual({
      action: { type: "SLOT", payload: { slot: "goals", value: ["child_education", "income_protection"] } },
    });
    const echo = document.querySelectorAll("li.you");
    expect(echo[echo.length - 1]?.textContent).toBe("You:Fund a child's education, Protect your family's income");
    // Answered: the picks stay visible, every button is off.
    expect(pressed(button(reply, "Fund a child's education"))).toBe("true");
    expect(within(reply).getAllByRole("button").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });

  it("a plain quick reply beside the toggles still sends its own action at once", async () => {
    const { api, reply } = await atGoals();
    fireEvent.click(button(reply, "Repay loans"));
    api.answer(json(200, NEXT));
    fireEvent.click(button(reply, "Talk to an advisor"));
    await waitFor(() => expect(replies()).toHaveLength(2));
    expect(sent(api).at(-1)).toEqual({ action: { type: "HUMAN_REQUEST", payload: {} } });
  });
});
