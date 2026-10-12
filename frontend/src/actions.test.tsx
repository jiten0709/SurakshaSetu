import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Chat } from "./Chat";
import type { TurnResponse } from "./client";
import { ACK_MISMATCH, ALTERNATIVES, CHOOSE, JOURNEY_DOWN, S3_HI, S3_RENDER, SELECTION } from "./fixtures";
import { fakeApi, json, paragraph } from "./test-support";

// S3's choices on bodies captured from the API: every quick reply sends its own action unchanged
// (the acknowledgment's hashes included), only the latest reply's buttons work, and a re-render
// leaves the older recommendation in the log, muted, with its buttons off.

const replies = () => [...document.querySelectorAll<HTMLElement>("li.server")];
const latest = () => replies().at(-1) as HTMLElement;
const buttons = (reply: HTMLElement) => within(reply).queryAllByRole("button") as HTMLButtonElement[];
const labels = (body: TurnResponse) => body.message.quick_replies.map((q) => q.label);
const action = (body: TurnResponse, label: string) => body.message.quick_replies.find((q) => q.label === label)?.action;

async function inS3() {
  const api = fakeApi();
  api.answer(json(200, S3_RENDER));
  render(<Chat busyRetryMs={0} />);
  await waitFor(() => expect(replies()).toHaveLength(1));
  return api;
}

/** Clicks a quick reply of the latest reply, the API answering `next`; waits for the answer. */
async function press(api: ReturnType<typeof fakeApi>, label: string, next: TurnResponse) {
  api.answer(json(200, next));
  const count = replies().length;
  fireEvent.click(within(latest()).getByRole("button", { name: label }));
  await waitFor(() => expect(replies()).toHaveLength(count + 1));
  await waitFor(() => expect(buttons(latest()).every((b) => !b.disabled)).toBe(true));
}

const sent = (api: ReturnType<typeof fakeApi>) => api.turns().at(-1)?.body;

describe("the call to action", () => {
  it("offers the server's four choices in its order; Apply sends APPLY as offered", async () => {
    const api = await inS3();
    const group = within(latest()).getByRole("group", { name: "Choices" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(labels(S3_RENDER));
    expect(labels(S3_RENDER)).toHaveLength(4);
    await press(api, "Apply for one of these plans", CHOOSE);
    expect(sent(api)).toEqual({ action: { type: "APPLY", payload: {} } });
    expect(sent(api)).toEqual({ action: action(S3_RENDER, "Apply for one of these plans") });
  });

  it("asks which plan with the server's own quick replies: APPLY and CHEAPER per option, sent unchanged", async () => {
    const api = await inS3();
    await press(api, "Apply for one of these plans", CHOOSE);
    expect(buttons(latest()).map((b) => b.textContent)).toEqual(labels(CHOOSE));
    await press(api, "Lower-cost versions of Suraksha Term Shield", ALTERNATIVES);
    expect(sent(api)).toEqual({ action: { type: "CHEAPER", payload: { uin: "999N001V02" } } });
    expect(screen.getByText(paragraph(ALTERNATIVES.message.parts[0]!.text))).toBeTruthy();
    expect(buttons(replies()[1]!).every((b) => b.disabled)).toBe(true); // the plan choice is spent
    await press(api, labels(ALTERNATIVES)[0]!, SELECTION);
    expect(sent(api)).toEqual({ action: ALTERNATIVES.message.quick_replies[0]!.action });
  });
});

describe("the acknowledgment", () => {
  it("sends DISCLOSURE_ACK exactly as offered, the shown hashes untouched", async () => {
    const api = await inS3();
    await press(api, "Apply for one of these plans", CHOOSE);
    await press(api, "Apply for Suraksha Term Shield", SELECTION);
    expect(sent(api)).toEqual({ action: { type: "APPLY", payload: { uin: "999N001V02" } } });
    const ack = SELECTION.message.quick_replies.find((q) => q.action.type === "DISCLOSURE_ACK");
    if (!ack) throw new Error("no acknowledgment offered");
    await press(api, ack.label, ACK_MISMATCH);
    expect(sent(api)).toEqual({ action: ack.action });
    expect(JSON.stringify(sent(api))).toBe(JSON.stringify({ action: ack.action })); // same keys, same order
  });

  it("on a mismatch shows the options again: the old render muted with its buttons off, the new one live", async () => {
    const api = await inS3();
    await press(api, "Apply for one of these plans", CHOOSE);
    await press(api, "Apply for Suraksha Term Shield", SELECTION);
    await press(api, "I have read them", ACK_MISMATCH);
    const [first, choose, selection, again] = replies();
    expect(screen.getByText(paragraph(ACK_MISMATCH.message.parts[0]!.text))).toBeTruthy();
    expect(first?.classList.contains("superseded")).toBe(true);
    for (const reply of [choose, selection, again]) expect(reply?.classList.contains("superseded")).toBe(false);
    expect(within(again!).getAllByRole("article")).toHaveLength(2);
    expect(within(again!).getAllByRole("region")).toHaveLength(2); // the disclosures shown again, whole
    for (const reply of [first, choose, selection]) expect(buttons(reply!).every((b) => b.disabled)).toBe(true);
    expect(buttons(again!).map((b) => b.disabled)).toEqual([false, false, false, false]);
  });
});

describe("a re-render", () => {
  it("after a language switch shows the Hindi cards live and mutes the English ones", async () => {
    const api = await inS3();
    api.answer(json(200, S3_HI));
    fireEvent.click(screen.getByRole("button", { name: "हिंदी" }));
    await waitFor(() => expect(replies()).toHaveLength(2));
    expect(sent(api)).toEqual({ action: { type: "LANGUAGE", payload: { locale: "hi-IN" } } });
    const [english, hindi] = replies();
    expect(english?.classList.contains("superseded")).toBe(true);
    expect(hindi?.classList.contains("superseded")).toBe(false);
    expect(buttons(english!).every((b) => b.disabled)).toBe(true);
    await waitFor(() => expect(buttons(hindi!).every((b) => !b.disabled)).toBe(true));
    const heads = within(hindi!).getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(heads).toEqual(S3_HI.message.parts.filter((p) => p.id.startsWith("disclosures:")).map((p) => p.text.split("\n")[0]));
  });
});

describe("the application journey down", () => {
  it("keeps S3 open with the server's message and no choices of its own", async () => {
    const api = await inS3();
    await press(api, "Apply for one of these plans", CHOOSE);
    await press(api, "Apply for Suraksha Term Shield", SELECTION);
    api.answer(json(200, JOURNEY_DOWN));
    fireEvent.click(within(latest()).getByRole("button", { name: "I have read them" }));
    await waitFor(() => expect(replies()).toHaveLength(4));
    for (const p of JOURNEY_DOWN.message.parts) expect(screen.getByText(paragraph(p.text))).toBeTruthy();
    expect(JOURNEY_DOWN.state).toBe("S3");
    expect(screen.queryByText("This conversation has ended.")).toBeNull();
    await waitFor(() => expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(false));
    expect(buttons(latest())).toEqual([]);
  });
});
