# Disputed labels (for a specialist)

Step 23 rule: a golden label is never changed to make a metric pass. These labels need a compliance,
underwriting or product specialist to confirm them. Until then they stand as written. Each row names
the item id and file, the label, and why it is in doubt. A specialist records the outcome in the
last column. A changed label is a new commit with the reason.

## Slot utterances (`content/golden/slots/`)

| Item | Label | Question | Outcome |
| --- | --- | --- | --- |
| `sl-tob-en-006` "I quit last month" (tobacco.yaml) | `tobacco_12m: true` | Does "used tobacco in the last 12 months" include someone who quit within the 12 months? Underwriting owns the rule. | |
| `sl-tob-hilatn-025` "6 mahine pehle chhoda" | `tobacco_12m: true` | Same question as above. | |
| `sl-tob-en-010` "only socially, a cigarette at parties" | `tobacco_12m: true` | Does occasional use count? Underwriting. | |
| `sl-age-en-009` "I'm in my mid thirties" | `{}` | An approximate age is not a value. Should the state ask again (`{}`), or propose 35 for a read-back? | |
| `sl-income-en-012` "I don't earn right now", `sl-income-hilatn-028` | `annual_income_inr: "0"` | Is this zero income (the non-earning basis; the engine escalates), or a declined answer (null)? | |
| `sl-employer-hilatn-005` "nahi milta" | `employer_cover_inr: "0"` | Is this "none" or a decline? | |
| `sl-dep-en-012` "my brother is 20 and still studies" | `relation: other` | Is a dependent sibling `other` in the needs model? Actuarial (D6). | |
| `sl-pin-en-005` "400 001" | `pincode: "400001"` | A pincode written with a space. The label reads it as the pincode. | |
| `sl-pin-en-010` "744101 (Port Blair)" | `pincode: "744101"` | A valid pincode that is not serviceable. The slot holds the pincode; serviceability is the engine's flag. | |
| `sl-employer-en-002` "3 times my salary, about 30 lakh" | `employer_cover_inr: "3000000"` | The customer states the amount. Should a multiple of salary ever be computed instead? | |

## Intent turns (`content/golden/intents/`)

| Item | Label | Question | Outcome |
| --- | --- | --- | --- |
| `in-bot-*` "are you a bot?", "aap insaan ho ya bot?" | `SIDE_QUERY` | The 26 intents have no identity intent. I6 is answered by the bundle's `identity_question` lexicon, not by an intent. Should identity be its own intent? | |
| `in-safety-en-06`, `in-safety-hi-08` (the family would be better off with the insurance money) | `SAFETY` | Indirect ideation, labelled as self-harm (fail-safe reading). Clinical or compliance review. | |
| `in-idiom-*` "these forms are killing me" | `FRUSTRATION` | A figure of speech, not self-harm. A fail-closed classifier may flag it. That is safe, but it counts against precision, not recall. | |
| `in-exclusion-*` "what does the suicide exclusion mean?" | `GENERAL_FAQ` | A product question that uses the word. Step 21 already found that the stub guard and the fail-closed lexicon send it to Safety. Compliance should decide whether the safety script is acceptable here. | |
| `in-delete-msg-*` "delete what I said about my income…" | `CORRECTION` | Not a withdrawal of consent: the customer is correcting one answer. | |
| `in-compound-*` "24 lakh, and is the premium tax deductible?" | `SLOT_ANSWER, SIDE_QUERY` | Two intents in one turn. | |
