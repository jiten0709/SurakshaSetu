import { expect, test } from "@playwright/test";

// One real-browser pass from the greeting to the signed hand-off (Step 30, `make ui-e2e`). The answers
// are the scripted customer's (content/golden/preludes/to-s3.yaml, then scripted_s0_s3.yaml); each
// step names the part the server's reply must carry. S3 follows the server's own quick replies (no UI
// control sends the scripted custom rider set; Step 29 X1). A failure names the step only: no
// message text reaches the output.

type Step = [kind: "say" | "pick", what: string, part: string];

const STEPS: Step[] = [
  ["pick", "Buy new cover", "template:RL-S1-AGE"],
  ["say", "34", "template:RL-S1-RESIDENCY"],
  ["pick", "Resident in India", "template:RL-S1-PINCODE"],
  ["say", "411001", "template:RL-S1-TOBACCO"],
  ["say", "no", "template:RL-S1-OCCUPATION"],
  ["say", "salaried", "template:RL-S1-HEALTH"],
  ["say", "no", "template:RL-S1-PROPOSER"],
  ["say", "yes", "template:readback"],
  ["say", "yes", "template:RL-S2-GOALS"],
  ["say", "income protection and loan cover", "template:RL-S2-INCOME"],
  ["say", "24 lakh", "template:readback"],
  ["say", "yes", "template:RL-S2-INCOME-TYPE"],
  ["pick", "Salaried", "template:RL-S2-DEPENDANTS"],
  ["say", "my wife is 32, my son is 7 and my daughter is 4", "template:RL-S2-LIABILITIES"],
  ["say", "home loan of 35 lakh, 18 years left", "template:readback"],
  ["say", "yes", "template:RL-S2-EXISTING-COVER"],
  ["say", "none", "template:RL-S2-EMPLOYER-COVER"],
  ["say", "15 lakh", "template:readback"],
  ["say", "yes", "template:RL-S2-EXISTING-PREMIUM"],
  ["say", "none", "template:RL-S2-ASSETS"],
  ["pick", "Skip", "template:RL-S2-BUDGET"],
  ["say", "30,000 a year", "template:readback"],
  ["say", "yes", "template:needs_summary"],
  ["say", "yes", "option_card:999N001V02"],
  ["pick", "Apply for one of these plans", "template:choose_option"],
  ["pick", "Apply for Suraksha Term Shield", "template:ack_ask"],
  ["pick", "I have read them", "template:handoff_application"],
];

// `make serve` allows 20 turns a minute in fixed windows; one turn every 3.1 s stays under it.
const PACE_MS = 3_100;

test("the scripted customer reaches HANDOFF in the browser", async ({ page }) => {
  const replies = page.locator("ol.log > li.server");
  let at = "opening the chat";
  let sent = Date.now();

  /** Does `act`, then waits for one more reply that carries `part`. */
  async function turn(act: () => Promise<void>, part: string) {
    const before = await replies.count();
    await page.waitForTimeout(Math.max(0, sent + PACE_MS - Date.now()));
    sent = Date.now();
    await act();
    await expect(replies).toHaveCount(before + 1);
    await expect(replies.last().locator(`[data-part="${part}"]`)).toHaveCount(1);
  }

  const say = (text: string) => async () => {
    await page.getByLabel("Your message").fill(text);
    await page.getByRole("button", { name: "Send", exact: true }).click();
  };
  const pick = (label: string) => () => replies.last().getByRole("button", { name: label, exact: true }).click();

  try {
    await page.goto("/"); // the chat sends START itself
    await expect(replies.last().locator('[data-part="template:greeting"]')).toHaveCount(1);

    at = "consent";
    const form = page.getByRole("form", { name: "Consent form" });
    await turn(async () => {
      for (const box of await form.getByRole("checkbox").all()) await box.check();
      await form.getByRole("button").click();
    }, "template:intent_ask");

    for (const [i, [kind, what, part]] of STEPS.entries()) {
      at = `step ${i + 1} (${part})`;
      await turn(kind === "say" ? say(what) : pick(what), part);
      if (part.startsWith("option_card:")) {
        at = "the S3 render at phone width";
        await page.setViewportSize({ width: 390, height: 844 });
        const wide = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
        expect(wide).toBe(false);
        await page.setViewportSize({ width: 1280, height: 720 });
      }
    }

    at = "the ended conversation";
    await expect(page.getByText("This conversation has ended.", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "New conversation" })).toBeEnabled();
  } catch {
    // The UI's notices are fixed strings, never message text; the caught error (which may quote the
    // page) is dropped on purpose.
    const notice = await page.getByRole("alert").textContent({ timeout: 1_000 }).catch(() => null);
    throw new Error(`smoke failed at ${at}${notice ? `; the UI says: ${notice}` : ""}`);
  }
});
