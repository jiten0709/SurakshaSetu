import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Chat } from "./Chat";
import { DECLINED, GREETING_EN, GREETING_HI, S1_ASK, S1_HI } from "./fixtures";
import cssSource from "./index.css?raw";
import { deferred, fakeApi, json, paragraph, problem } from "./test-support";

// The language switch: NOTICE_LANGUAGE while S0's consent form is open, LANGUAGE afterwards (the
// spec's actions); the document `lang` follows the session's locale as the server answered it.

const lang = () => document.documentElement.lang;
const toggle = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

async function opened(first: Response, ...more: (Response | Error)[]) {
  const api = fakeApi();
  api.answer(first, ...more);
  render(<Chat busyRetryMs={0} />);
  await waitFor(() => expect(document.querySelector("li.server")).not.toBeNull());
  return api;
}

describe("switching", () => {
  it("while the consent form is open, sends NOTICE_LANGUAGE and shows the notice and form in Hindi", async () => {
    const api = await opened(json(200, GREETING_EN), json(200, GREETING_HI));
    expect(lang()).toBe("en-IN");
    fireEvent.click(toggle("हिंदी"));
    await screen.findByText(paragraph(GREETING_HI.message.parts.at(-1)?.text ?? ""));
    expect(api.turns()[1]?.body).toEqual({ action: { type: "NOTICE_LANGUAGE", payload: { language: "hi-IN" } } });
    expect(screen.getByLabelText(GREETING_HI.message.form.adult.label)).toBeTruthy();
    expect(lang()).toBe("hi-IN");
    expect(toggle("English").lang).toBe("en-IN");
  });

  it("after the form, sends LANGUAGE; lang moves only when the server answers", async () => {
    const api = await opened(json(200, S1_ASK), problem(503, "SERVICE_UNAVAILABLE"), json(200, S1_HI));
    fireEvent.click(toggle("हिंदी"));
    await screen.findByRole("alert");
    expect(api.turns()[1]?.body).toEqual({ action: { type: "LANGUAGE", payload: { locale: "hi-IN" } } });
    expect(lang()).toBe("en-IN");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(paragraph(S1_HI.message.parts.at(-1)?.text ?? ""));
    expect(lang()).toBe("hi-IN");
  });

  it("the server's own 'Read in' quick reply moves lang too (the form names its language)", async () => {
    await opened(json(200, GREETING_EN), json(200, GREETING_HI));
    const reply = GREETING_EN.message.quick_replies[0];
    fireEvent.click(screen.getByRole("button", { name: reply?.label }));
    await screen.findByText(paragraph(GREETING_HI.message.parts.at(-1)?.text ?? ""));
    expect(lang()).toBe("hi-IN");
  });

  it("is off while a turn runs and once the conversation has closed", async () => {
    const api = await opened(json(200, GREETING_EN));
    const answer = deferred();
    api.answer(answer.promise);
    fireEvent.click(screen.getByRole("button", { name: GREETING_EN.message.form.submit }));
    expect(toggle("हिंदी").disabled).toBe(true);
    answer.resolve(json(200, DECLINED));
    await screen.findByText(paragraph(DECLINED.message.parts[0]?.text ?? ""));
    expect(toggle("हिंदी").disabled).toBe(true);
  });

  it("a new conversation opens in the language chosen", async () => {
    const api = await opened(json(200, GREETING_EN), json(200, GREETING_HI), json(200, DECLINED), json(200, GREETING_HI));
    fireEvent.click(toggle("हिंदी"));
    await screen.findByText(paragraph(GREETING_HI.message.parts.at(-1)?.text ?? ""));
    fireEvent.click(screen.getAllByRole("button", { name: GREETING_HI.message.form.submit }).at(-1) as HTMLElement);
    fireEvent.click(await screen.findByRole("button", { name: "New conversation" }));
    await screen.findByText(paragraph(GREETING_HI.message.parts.at(-1)?.text ?? ""));
    expect(api.calls.filter((c) => c.url === "/v1/sessions").map((c) => c.body)).toEqual([
      { channel: "web", locale: "en-IN" },
      { channel: "web", locale: "hi-IN" },
    ]);
  });
});

it("the font stack names a Devanagari face (local fonts only, nothing fetched)", () => {
  expect(cssSource).toMatch(/font:[^;]*"Noto Sans Devanagari"[^;]*;/);
  expect(cssSource).not.toMatch(/@import|@font-face|url\(/);
});
