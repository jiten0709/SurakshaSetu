import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import chatSource from "./Chat.tsx?raw";
import clientSource from "./client.ts?raw";
import { Chat } from "./Chat";
import { DECLINED, GREETING_EN, GREETING_HI, INTENT_ASK, NOTICE_UPDATED, S1_ASK } from "./fixtures";
import cssSource from "./index.css?raw";
import mainSource from "./main.tsx?raw";
import htmlSource from "../index.html?raw";
import { fakeApi, json, paragraph, problem, turn } from "./test-support";

// The S0 consent form: rendered only from a server `form`, its labels exactly as sent, submitted as
// CONSENT_SUBMIT with the form's own notice fields. The UI hashes and validates nothing.

const FORM = GREETING_EN.message.form;
const label = (id: "P1" | "P2" | "P3", form = FORM) => form.purposes.find((p) => p.id === id)?.label ?? "";

async function greeted(...more: (Response | Error)[]) {
  const api = fakeApi();
  api.answer(json(200, GREETING_EN), ...more);
  render(<Chat busyRetryMs={0} />);
  await screen.findByRole("form", { name: "Consent form" });
  return api;
}

function controls(form: HTMLElement) {
  return [...within(form).getAllByRole("checkbox"), within(form).getByRole("button")] as HTMLInputElement[];
}

describe("rendering", () => {
  it("shows every part verbatim, then the server's form with nothing ticked", async () => {
    await greeted();
    const shown = [...document.querySelectorAll("li.server p")].map((p) => p.textContent);
    expect(shown).toEqual(GREETING_EN.message.parts.map((p) => p.text)); // the notice and AI disclosure untouched
    const form = screen.getByRole("form", { name: "Consent form" });
    const boxes = within(form).getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.map((b) => b.labels?.[0]?.textContent)).toEqual([...FORM.purposes.map((p) => p.label), FORM.adult.label]);
    expect(boxes.map((b) => b.checked)).toEqual([false, false, false, false]); // P1 is not preselected
    expect(within(form).getByRole("button").textContent).toBe(FORM.submit);
  });

  it.each([
    ["the intent question", INTENT_ASK],
    ["an S1 question", S1_ASK],
    ["a synthetic reply", turn("S0", ["Plain reply"])],
  ])("never renders a form without a server form: %s", async (_, body) => {
    const api = fakeApi();
    api.answer(json(200, body));
    render(<Chat busyRetryMs={0} />);
    await screen.findByText(paragraph(body.message.parts.at(-1)?.text ?? ""));
    expect(screen.queryByRole("form", { name: "Consent form" })).toBeNull();
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
  });
});

describe("submitting", () => {
  it("sends CONSENT_SUBMIT with the ticks and the form's own notice, then stays disabled; Retry resends it", async () => {
    const api = await greeted(problem(503, "SERVICE_UNAVAILABLE"), json(200, INTENT_ASK));
    fireEvent.click(screen.getByLabelText(label("P1")));
    fireEvent.click(screen.getByLabelText(label("P3")));
    fireEvent.click(screen.getByLabelText(FORM.adult.label));
    fireEvent.click(screen.getByRole("button", { name: FORM.submit }));
    await screen.findByRole("alert");
    const form = screen.getByRole("form", { name: "Consent form" });
    expect(controls(form).map((c) => c.disabled)).toEqual([true, true, true, true, true]);
    const submitted = api.turns()[1];
    expect(submitted?.body).toEqual({
      action: {
        type: "CONSENT_SUBMIT",
        payload: {
          purposes: { P1: true, P2: false, P3: true },
          age_18_plus: true,
          notice_version: FORM.notice_version,
          notice_sha256: FORM.notice_sha256,
        },
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(paragraph(INTENT_ASK.message.parts[0]?.text ?? ""));
    const retried = api.turns()[2];
    expect(retried?.body).toEqual(submitted?.body);
    expect(retried?.headers["idempotency-key"]).toBe(submitted?.headers["idempotency-key"]);
    expect(controls(form).every((c) => c.disabled)).toBe(true);
  });

  it("submits with P1 unticked (the server answers): the refusal's text and a new conversation", async () => {
    const api = await greeted(json(200, DECLINED));
    fireEvent.click(screen.getByLabelText(FORM.adult.label));
    fireEvent.click(screen.getByRole("button", { name: FORM.submit }));
    await screen.findByText(paragraph(DECLINED.message.parts[0]?.text ?? ""));
    const payload = (api.turns()[1]?.body as { action: { payload: { purposes: unknown } } }).action.payload;
    expect(payload.purposes).toEqual({ P1: false, P2: false, P3: false });
    expect(screen.getByRole("button", { name: "New conversation" })).toBeTruthy();
    expect((screen.getByLabelText("Your message") as HTMLInputElement).disabled).toBe(true);
  });

  it("NOTICE_MISMATCH: the re-rendered form is live, the old one is not, and it submits the new notice", async () => {
    const newer = structuredClone(NOTICE_UPDATED);
    newer.message.form.notice_version = "2026.10.1-en";
    newer.message.form.notice_sha256 = "f".repeat(64);
    const api = await greeted(json(200, newer), json(200, INTENT_ASK));
    fireEvent.click(screen.getByLabelText(label("P1")));
    fireEvent.click(screen.getByLabelText(FORM.adult.label));
    fireEvent.click(screen.getByRole("button", { name: FORM.submit }));
    await waitFor(() => expect(screen.getAllByRole("form", { name: "Consent form" })).toHaveLength(2));
    const [old, current] = screen.getAllByRole("form", { name: "Consent form" }) as HTMLElement[];
    expect(controls(old as HTMLElement).every((c) => c.disabled)).toBe(true);
    expect(controls(current as HTMLElement).some((c) => c.disabled)).toBe(false);
    fireEvent.click(within(current as HTMLElement).getByLabelText(label("P1")));
    fireEvent.click(within(current as HTMLElement).getByLabelText(FORM.adult.label));
    fireEvent.click(within(current as HTMLElement).getByRole("button"));
    await screen.findByText(paragraph(INTENT_ASK.message.parts[0]?.text ?? ""));
    const payload = (api.turns()[2]?.body as { action: { payload: Record<string, unknown> } }).action.payload;
    expect([payload.notice_version, payload.notice_sha256]).toEqual(["2026.10.1-en", "f".repeat(64)]);
  });
});

describe("nothing hard-coded", () => {
  it("the form's whole text is the server's labels", async () => {
    const body = turn("S0", ["Plain reply"]);
    body.message.form = {
      type: "CONSENT_SUBMIT",
      notice_version: "v-SENTINEL",
      notice_sha256: "a".repeat(64),
      language: "en-IN",
      purposes: [
        { id: "P1", label: "One-SENTINEL", required: true },
        { id: "P2", label: "Two-SENTINEL", required: false },
        { id: "P3", label: "Three-SENTINEL", required: false },
      ],
      adult: { label: "Adult-SENTINEL" },
      submit: "Submit-SENTINEL",
    };
    const api = fakeApi();
    api.answer(json(200, body));
    render(<Chat busyRetryMs={0} />);
    const form = await screen.findByRole("form", { name: "Consent form" });
    expect(form.textContent).toBe("One-SENTINELTwo-SENTINELThree-SENTINELAdult-SENTINELSubmit-SENTINEL");
  });

  it("the UI's source holds no notice line and no form label", () => {
    const files = [chatSource, clientSource, mainSource, cssSource, htmlSource];
    expect(files.every((f) => f.length > 100)).toBe(true); // each was really read
    const sources = files.join("\n");
    const legal = [GREETING_EN, GREETING_HI].flatMap((body) => [
      ...body.message.parts.filter((p) => !p.id.startsWith("template:")).flatMap((p) => p.text.split("\n")),
      ...body.message.form.purposes.map((p) => p.label),
      body.message.form.adult.label,
    ]);
    const lines = legal.map((line) => line.trim()).filter((line) => line.length >= 12);
    expect(lines.length).toBeGreaterThan(10);
    expect(lines.filter((line) => sources.includes(line))).toEqual([]);
  });
});
