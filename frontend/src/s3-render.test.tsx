import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import chatSource from "./Chat.tsx?raw";
import { Chat } from "./Chat";
import type { TurnResponse } from "./client";
import { S3_RENDER, TAX_ANSWER } from "./fixtures";
import cssSource from "./index.css?raw";
import { fakeApi, json, lines, paragraph, shownParts, turn } from "./test-support";

// The S3 recommendation rendered by part id, on a body captured from the API: every part shown, in
// the order sent, its text as sent. Disclosures are legal text: whole, in order, never collapsed.

async function opened(body: TurnResponse = S3_RENDER) {
  const api = fakeApi();
  api.answer(json(200, body));
  render(<Chat busyRetryMs={0} />);
  const reply = await waitFor(() => {
    const li = document.querySelector("li.server");
    if (!li) throw new Error("no reply yet");
    return li;
  });
  return { api, reply };
}

const PARTS = S3_RENDER.message.parts;
const part = (id: string) => PARTS.find((p) => p.id === id)?.text ?? "";
const ids = (prefix: string) => PARTS.filter((p) => p.id.startsWith(prefix)).map((p) => p.id);

describe("the recommendation", () => {
  it("shows every part, in the order sent", async () => {
    const { reply } = await opened();
    expect(shownParts(reply).map((e) => e.dataset.part)).toEqual(PARTS.map((p) => p.id));
  });

  it("keeps a shuffled order as sent (the UI never sorts)", async () => {
    const reversed = structuredClone(S3_RENDER);
    reversed.message.parts.reverse();
    const { reply } = await opened(reversed);
    expect(shownParts(reply).map((e) => e.dataset.part)).toEqual(reversed.message.parts.map((p) => p.id));
  });

  it("shows each option as a card headed by its first line, its text exactly as sent", async () => {
    await opened();
    const cards = screen.getAllByRole("article");
    expect(cards.map((c) => c.dataset.part)).toEqual(ids("option_card:"));
    for (const card of cards) {
      const text = part(card.dataset.part ?? "");
      expect(card.getAttribute("aria-label")).toBe(text.split("\n")[0]);
      expect(lines(card)).toBe(text); // the documents line included, beside its own option
    }
    expect(within(cards[0]!).getByRole("heading").textContent).toBe("Option 1: Suraksha Term Shield (999N001V02)");
  });

  it("shows the comparison as a table, every cell as sent", async () => {
    await opened();
    const text = part("comparison");
    const [caption, ...rows] = text.split("\n");
    const table = screen.getByRole("table", { name: caption });
    const sent = rows.filter((r) => !r.startsWith("|---")).map((r) => r.split("|").slice(1, -1).map((c) => c.trim()));
    const shown = [...(table as HTMLTableElement).rows].map((r) => [...r.cells].map((c) => c.textContent));
    expect(shown).toEqual(sent);
    expect(within(table).getAllByRole("columnheader").map((c) => c.textContent)).toEqual([
      "Suraksha Term Shield (999N001V02)",
      "Suraksha Term Shield ROP (999N002V01)",
    ]);
    expect(within(table).getAllByRole("rowheader").map((c) => c.textContent)).toEqual(sent.slice(1).map((r) => r[0]));
  });

  it("shows a comparison it cannot read as a table as plain text, whole", async () => {
    const odd = turn("S3", ["How the options compare\n| a | b |\nnot a row"]);
    odd.message.parts[0]!.id = "comparison";
    const { reply } = await opened(odd);
    expect(screen.queryByRole("table")).toBeNull();
    expect(shownParts(reply)[0]?.textContent).toBe("How the options compare\n| a | b |\nnot a row");
  });

  it("shows every disclosure set whole, in order, never collapsed or hidden", async () => {
    await opened();
    const sets = screen.getAllByRole("region");
    expect(sets.map((s) => s.dataset.part)).toEqual(ids("disclosures:"));
    for (const set of sets) {
      const text = part(set.dataset.part ?? "");
      expect(set.getAttribute("aria-label")).toBe(text.split("\n")[0]);
      expect(lines(set)).toBe(text);
      expect(set.closest("details, [hidden]")).toBeNull();
    }
    expect(document.querySelector("details")).toBeNull();
  });

  it("styles nothing that could clip or hide a disclosure", () => {
    expect(cssSource).not.toMatch(/max-height|line-clamp|text-overflow|display:\s*none|visibility:\s*hidden/);
    for (const [, rule] of cssSource.matchAll(/\.disclosures[^{]*\{([^}]*)\}/g)) {
      expect(rule).not.toMatch(/overflow|height/);
    }
  });

  it("keeps the [Source: …] labels of why it fits exactly as sent", async () => {
    const { reply } = await opened();
    const why = shownParts(reply).find((e) => e.dataset.part === "why_it_fits");
    expect(why?.tagName).toBe("P");
    expect(why?.textContent).toBe(part("why_it_fits"));
    expect(why?.textContent).toContain("[Source: PPI_MasterCircular_2024 §3]");
  });

  it("shows the sources as a panel, one line per source as sent", async () => {
    await opened();
    const panel = screen.getByRole("complementary", { name: "Sources" });
    expect(lines(panel)).toBe(part("sources"));
    expect(within(panel).getAllByRole("listitem")).toHaveLength(S3_RENDER.message.sources.length);
  });

  it("shows the needs recap and the call to action as their text", async () => {
    const { reply } = await opened();
    for (const id of ["needs_recap", "cta"]) {
      const shown = shownParts(reply).find((e) => e.dataset.part === id);
      expect(shown?.tagName).toBe("P");
      expect(shown?.textContent).toBe(part(id));
    }
  });
});

describe("a side answer in S3", () => {
  it("shows its sources panel (template:sources) and the tax disclosure verbatim", async () => {
    const { reply } = await opened(TAX_ANSWER);
    const sent = (id: string) => TAX_ANSWER.message.parts.find((p) => p.id === id)?.text ?? "";
    expect(lines(screen.getByRole("complementary", { name: "Sources" }))).toBe(sent("template:sources"));
    expect(screen.getByText(paragraph(sent("registry:DISC-GLOBAL-TAX-05")))).toBeTruthy();
    expect(screen.getByText(paragraph(sent("generated:answer")))).toBeTruthy();
    expect(shownParts(reply).map((e) => e.dataset.part)).toEqual(TAX_ANSWER.message.parts.map((p) => p.id));
  });
});

describe("anything else", () => {
  it("shows an unknown part id as plain text, never hidden", async () => {
    const odd = turn("S3", ["A line\nand another"]);
    odd.message.parts[0]!.id = "mystery:thing";
    const { reply } = await opened(odd);
    const shown = shownParts(reply)[0];
    expect(shown?.tagName).toBe("P");
    expect(shown?.textContent).toBe("A line\nand another");
  });

  it("formats no money and sorts nothing: amounts arrive formatted in the text", () => {
    expect(chatSource).not.toMatch(/Intl\.NumberFormat|toLocaleString|₹|\.sort\(|\.toSorted\(/);
  });
});
