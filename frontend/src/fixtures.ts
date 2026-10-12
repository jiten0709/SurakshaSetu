// Released turns captured from the running API (make serve, pb-2026.10.8, the seed notices) on the
// s0 goldens' own turns (Step 28) and the s3 goldens' turns (Step 29, below), unchanged. Test-only:
// the app never imports this file. `satisfies` makes tsc check each body against the generated
// contract types. Recapture rather than hand-edit.
import type { TurnResponse } from "./client";

/** START (s0/full-consent turn 1). */
export const GREETING_EN = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": {
      "adult": {
        "label": "I confirm I am 18 or older"
      },
      "language": "en-IN",
      "notice_sha256": "2a24e966bfd870e6d6d4edab5598d06226576fc1a5c1e846b8fb957fb24ec31f",
      "notice_version": "2026.09.1-en",
      "purposes": [
        {
          "id": "P1",
          "label": "Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans",
          "required": true
        },
        {
          "id": "P2",
          "label": "Let a licensed advisor contact me",
          "required": false
        },
        {
          "id": "P3",
          "label": "Send me product updates",
          "required": false
        }
      ],
      "submit": "Continue",
      "type": "CONSENT_SUBMIT"
    },
    "parts": [
      {
        "id": "template:greeting",
        "text": "Namaste, I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor.\nI can help you work out how much life cover you may need and explain our plans.\nFinal terms are decided during application and underwriting. This chat is recorded."
      },
      {
        "id": "registry:DISC-GLOBAL-AI-06",
        "text": "DUMMY: AI assistant identity and human alternative."
      },
      {
        "id": "notice:2026.09.1-en",
        "text": "DUMMY: Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in हिंदी / other language]"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "language": "hi-IN"
          },
          "type": "NOTICE_LANGUAGE"
        },
        "label": "Read in हिंदी"
      }
    ],
    "sources": [],
    "text": "Namaste, I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor.\nI can help you work out how much life cover you may need and explain our plans.\nFinal terms are decided during application and underwriting. This chat is recorded.\n\nDUMMY: AI assistant identity and human alternative.\n\nDUMMY: Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in हिंदी / other language]"
  },
  "state": "S0",
  "turn_id": "01a12bd5-2bd7-7000-a680-8cf88f6bab6a"
} satisfies TurnResponse;

/** CONSENT_SUBMIT, every box ticked (s0/full-consent turn 2). */
export const INTENT_ASK = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:intent_ask",
        "text": "Are you looking to explore new life insurance plans, or do you have questions about an existing policy?"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "intent": "new_purchase"
          },
          "type": "INTENT"
        },
        "label": "Buy new cover"
      },
      {
        "action": {
          "payload": {
            "intent": "specific_plan"
          },
          "type": "INTENT"
        },
        "label": "Price a plan"
      },
      {
        "action": {
          "payload": {
            "intent": "general_faq"
          },
          "type": "INTENT"
        },
        "label": "General question"
      },
      {
        "action": {
          "payload": {
            "intent": "existing_policy"
          },
          "type": "INTENT"
        },
        "label": "Existing policy"
      }
    ],
    "sources": [],
    "text": "Are you looking to explore new life insurance plans, or do you have questions about an existing policy?"
  },
  "state": "S0",
  "turn_id": "01a12bd5-2d7d-7000-ac3a-90a59c15ea69"
} satisfies TurnResponse;

/** INTENT new_purchase (s0/full-consent turn 3). */
export const S1_ASK = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "generated:narrative",
        "text": "Thanks, that helps me understand what you are looking for."
      },
      {
        "id": "template:RL-S1-AGE",
        "text": "How old are you? Plans have age limits, and age affects the premium."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "Thanks, that helps me understand what you are looking for.\n\nHow old are you? Plans have age limits, and age affects the premium."
  },
  "state": "S1",
  "turn_id": "01a12bd5-2dd7-7000-9cc2-ed815581b551"
} satisfies TurnResponse;

/** LANGUAGE hi-IN in S1. */
export const S1_HI = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "generated:narrative",
        "text": "Thanks, that helps me understand what you are looking for."
      },
      {
        "id": "template:RL-S1-AGE",
        "text": "DUMMY: [hi translation pending approval] How old are you? DUMMY: [hi translation pending approval] Plans have age limits, and age affects the premium."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "Thanks, that helps me understand what you are looking for.\n\nDUMMY: [hi translation pending approval] How old are you? DUMMY: [hi translation pending approval] Plans have age limits, and age affects the premium."
  },
  "state": "S1",
  "turn_id": "01a12bd5-301c-7000-b24c-4502a7b1a3f5"
} satisfies TurnResponse;

/** DELETE /v1/sessions/{id} in S1. */
export const ERASED = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:erasure_done",
        "text": "DUMMY: [hi translation pending approval] Approved erasure confirmation pending. We have stopped processing your information and deleted this conversation and your answers. Any consent you gave is withdrawn. We keep an encrypted audit record of this conversation and your consent history, because the law requires it; they become unreadable when the retention period ends."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "DUMMY: [hi translation pending approval] Approved erasure confirmation pending. We have stopped processing your information and deleted this conversation and your answers. Any consent you gave is withdrawn. We keep an encrypted audit record of this conversation and your consent history, because the law requires it; they become unreadable when the retention period ends."
  },
  "state": "DATA_ERASURE",
  "turn_id": "01a12bd5-307f-7000-a78d-46da687aaf11"
} satisfies TurnResponse;

/** NOTICE_LANGUAGE hi-IN (s0/hindi-notice turn 2). */
export const GREETING_HI = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": {
      "adult": {
        "label": "DUMMY: [hi translation pending approval] I confirm I am 18 or older"
      },
      "language": "hi-IN",
      "notice_sha256": "da7945c4b15d797899a04f0a5ffd67ed06f20b1ff7ebe359ece95aafaf8abd0a",
      "notice_version": "2026.09.1-hi",
      "purposes": [
        {
          "id": "P1",
          "label": "DUMMY: [hi translation pending approval] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans",
          "required": true
        },
        {
          "id": "P2",
          "label": "DUMMY: [hi translation pending approval] Let a licensed advisor contact me",
          "required": false
        },
        {
          "id": "P3",
          "label": "DUMMY: [hi translation pending approval] Send me product updates",
          "required": false
        }
      ],
      "submit": "DUMMY: [hi translation pending approval] Continue",
      "type": "CONSENT_SUBMIT"
    },
    "parts": [
      {
        "id": "template:greeting",
        "text": "DUMMY: [hi translation pending approval] Namaste, I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor.\nI can help you work out how much life cover you may need and explain our plans.\nFinal terms are decided during application and underwriting. This chat is recorded."
      },
      {
        "id": "registry:DISC-GLOBAL-AI-06",
        "text": "DUMMY: [hi translation pending approval] AI assistant identity and human alternative."
      },
      {
        "id": "notice:2026.09.1-hi",
        "text": "DUMMY: [hi translation pending approval] Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in English / other language]"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "language": "en-IN"
          },
          "type": "NOTICE_LANGUAGE"
        },
        "label": "DUMMY: [hi translation pending approval] Read in English"
      }
    ],
    "sources": [],
    "text": "DUMMY: [hi translation pending approval] Namaste, I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor.\nI can help you work out how much life cover you may need and explain our plans.\nFinal terms are decided during application and underwriting. This chat is recorded.\n\nDUMMY: [hi translation pending approval] AI assistant identity and human alternative.\n\nDUMMY: [hi translation pending approval] Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in English / other language]"
  },
  "state": "S0",
  "turn_id": "01a12bd5-30e4-7000-aa1e-f34ef0d70f91"
} satisfies TurnResponse;

/** CONSENT_SUBMIT on a wrong notice hash (s0/notice-mismatch turn 2). */
export const NOTICE_UPDATED = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": {
      "adult": {
        "label": "I confirm I am 18 or older"
      },
      "language": "en-IN",
      "notice_sha256": "2a24e966bfd870e6d6d4edab5598d06226576fc1a5c1e846b8fb957fb24ec31f",
      "notice_version": "2026.09.1-en",
      "purposes": [
        {
          "id": "P1",
          "label": "Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans",
          "required": true
        },
        {
          "id": "P2",
          "label": "Let a licensed advisor contact me",
          "required": false
        },
        {
          "id": "P3",
          "label": "Send me product updates",
          "required": false
        }
      ],
      "submit": "Continue",
      "type": "CONSENT_SUBMIT"
    },
    "parts": [
      {
        "id": "template:notice_updated",
        "text": "DUMMY: Approved text pending. The privacy notice has been updated. Please review it and choose your options again."
      },
      {
        "id": "registry:DISC-GLOBAL-AI-06",
        "text": "DUMMY: AI assistant identity and human alternative."
      },
      {
        "id": "notice:2026.09.1-en",
        "text": "DUMMY: Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in हिंदी / other language]"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "language": "hi-IN"
          },
          "type": "NOTICE_LANGUAGE"
        },
        "label": "Read in हिंदी"
      }
    ],
    "sources": [],
    "text": "DUMMY: Approved text pending. The privacy notice has been updated. Please review it and choose your options again.\n\nDUMMY: AI assistant identity and human alternative.\n\nDUMMY: Please review how we use your information (notice 2026.09.1).\n[P1 required] Use my answers, including basic health and lifestyle details, to assess my needs and suggest plans\n[P2 optional] Let a licensed advisor contact me\n[P3 optional] Send me product updates\n[ ] I confirm I am 18 or older\n[Read in हिंदी / other language]"
  },
  "state": "S0",
  "turn_id": "01a12bd5-3153-7000-a2e6-a8dd8829408e"
} satisfies TurnResponse;

/** CONSENT_SUBMIT with P1 unticked (s0/p1-declined turn 2). */
export const DECLINED = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:consent_declined",
        "text": "DUMMY: Approved helpline and branch-locator text pending. No problem: I won't collect any information. You can reach us through our helpline or at any branch."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "DUMMY: Approved helpline and branch-locator text pending. No problem: I won't collect any information. You can reach us through our helpline or at any branch."
  },
  "state": "EXIT",
  "turn_id": "01a12bd5-31c9-7000-af42-ab013091a6fe"
} satisfies TurnResponse;

// Step 29: S3 bodies captured from the running API (make serve, pb-2026.10.8, the DUMMY seed) on the
// s3 goldens' turns after the to-s3 prelude (full consent, so P2 is granted), unchanged. Typed as
// TurnResponse (still checked against the contract): the tests pass them where one is expected, and
// `satisfies` would keep an inferred quick-reply union that is not assignable to it.

/** S3 entered: "yes" to the needs summary (the to-s3 prelude, then the scripted conversation's turn 1). */
export const S3_RENDER: TurnResponse = {
  "documents": [
    {
      "documents": {
        "CIS": "9b12ebd6afb8d049704ad02679a8bcd1d6bccc83435e38abf465e98776782eae",
        "POLICY_WORDING": "e97977657813a04229582b30bafa45347ffc4fd47c1a08ed46e9d3949af138ff"
      },
      "uin": "999N001V02"
    },
    {
      "documents": {
        "CIS": "30948ccef5b45066a6e24d0d37ab1dbae16ba478c9ec781dc9f859e4d216fd06",
        "POLICY_WORDING": "993670f893e2b73bada887441736ccdb954b4bfe0519dc2309c9042e831a4167"
      },
      "uin": "999N002V01"
    }
  ],
  "message": {
    "citations": [
      {
        "handle": "R1",
        "ref": "RANK-FIT-TERM"
      },
      {
        "handle": "E1",
        "ref": "regulatory:irdai-ppi-mc-2024:3:f51511"
      },
      {
        "handle": "E2",
        "ref": "product:999N002V01:pw-v2:3:d3a8cc"
      },
      {
        "handle": "E3",
        "ref": "product:999N001V02:pw-v2:3:411cde"
      },
      {
        "handle": "R2",
        "ref": "RANK-FIT-TERM_ROP"
      },
      {
        "handle": "R3",
        "ref": "FIT-01,FIT-02,FIT-03,MA-01,AFL-01,AFF-01"
      }
    ],
    "cta": null,
    "disclosures": [
      {
        "set_sha256": "fdcc70b9a658fda8ce69daf5fec4c0420de4a784911541b292a0e53e5450eee8",
        "uin": "999N001V02"
      },
      {
        "set_sha256": "49177622f860773b0b62c67217e0334904761baf9e3d95fe02a2366e4ff003c6",
        "uin": "999N002V01"
      }
    ],
    "form": null,
    "parts": [
      {
        "id": "template:needs_done",
        "text": "Thank you. I have what I need to look for plans that fit."
      },
      {
        "id": "needs_recap",
        "text": "Your needs, as you confirmed them\nGoals: Protect your family's income, Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year."
      },
      {
        "id": "option_card:999N001V02",
        "text": "Option 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0001, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)"
      },
      {
        "id": "option_card:999N002V01",
        "text": "Option 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nIndicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0002, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)"
      },
      {
        "id": "comparison",
        "text": "How the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| Plan type | Term | Term with return of premium |\n| Entry age (years) | 18–65 | 18–55 |\n| Maximum age at maturity (years) | 85 | 75 |\n| Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| Policy term (years) | 10–40 | 15–35 |\n| Premium-paying options | Regular pay, Limited pay (10 years), Single premium | Regular pay, Limited pay (10 years) |\n| Benefit payment options | Lump sum, Monthly income | Lump sum |\n| Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |"
      },
      {
        "id": "why_it_fits",
        "text": "Option [Source: Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide."
      },
      {
        "id": "disclosures:999N001V02",
        "text": "Important information about Suraksha Term Shield (999N001V02)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change."
      },
      {
        "id": "disclosures:999N002V01",
        "text": "Important information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change."
      },
      {
        "id": "cta",
        "text": "What would you like to do next?\n- Apply for one of these plans\n- Talk to a licensed advisor\n- Change my answers\n- Save for later"
      },
      {
        "id": "sources",
        "text": "Sources\nIRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nSuraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nSuraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {},
          "type": "APPLY"
        },
        "label": "Apply for one of these plans"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "Talk to a licensed advisor"
      },
      {
        "action": {
          "payload": {},
          "type": "REVISE"
        },
        "label": "Change my answers"
      },
      {
        "action": {
          "payload": {},
          "type": "SAVE"
        },
        "label": "Save for later"
      }
    ],
    "sources": [
      {
        "effective_from": "2024-09-05",
        "section": "3. Customer Information Sheet",
        "title": "IRDAI › Master Circular on Protection of Policyholders' Interests 2024",
        "uri": "content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3",
        "version": "2024"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield ROP (999N002V01) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N002V01-policy-wording-v2.md#3",
        "version": "v2"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield (999N001V02) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N001V02-policy-wording-v2.md#3",
        "version": "v2"
      }
    ],
    "text": "Thank you. I have what I need to look for plans that fit.\n\nYour needs, as you confirmed them\nGoals: Protect your family's income, Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year.\n\nOption 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0001, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)\n\nOption 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nIndicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0002, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)\n\nHow the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| Plan type | Term | Term with return of premium |\n| Entry age (years) | 18–65 | 18–55 |\n| Maximum age at maturity (years) | 85 | 75 |\n| Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| Policy term (years) | 10–40 | 15–35 |\n| Premium-paying options | Regular pay, Limited pay (10 years), Single premium | Regular pay, Limited pay (10 years) |\n| Benefit payment options | Lump sum, Monthly income | Lump sum |\n| Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |\n\nOption [Source: Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nImportant information about Suraksha Term Shield (999N001V02)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change.\n\nImportant information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change.\n\nWhat would you like to do next?\n- Apply for one of these plans\n- Talk to a licensed advisor\n- Change my answers\n- Save for later\n\nSources\nIRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nSuraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nSuraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
  },
  "state": "S3",
  "turn_id": "01a12ec5-2e4c-7000-bfcb-95ffbbd61c38"
};

/** The CTA's APPLY {} with two options: which plan (APPLY and CHEAPER per option). */
export const CHOOSE: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:choose_option",
        "text": "Which plan would you like to apply for? You can also ask for a lower-cost version of one."
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Apply for Suraksha Term Shield"
      },
      {
        "action": {
          "payload": {
            "uin": "999N001V02"
          },
          "type": "CHEAPER"
        },
        "label": "Lower-cost versions of Suraksha Term Shield"
      },
      {
        "action": {
          "payload": {
            "uin": "999N002V01"
          },
          "type": "APPLY"
        },
        "label": "Apply for Suraksha Term Shield ROP"
      },
      {
        "action": {
          "payload": {
            "uin": "999N002V01"
          },
          "type": "CHEAPER"
        },
        "label": "Lower-cost versions of Suraksha Term Shield ROP"
      }
    ],
    "sources": [],
    "text": "Which plan would you like to apply for? You can also ask for a lower-cost version of one."
  },
  "state": "S3",
  "turn_id": "01a12ec5-41e5-7000-b998-344a373bd486"
};

/** CHEAPER 999N001V02 from CHOOSE's quick reply: the engine's lower-cost versions. */
export const ALTERNATIVES: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:alternatives",
        "text": "Here are lower-cost versions of Suraksha Term Shield (999N001V02), each with its protection gap against your recommended cover:\n1. Lower cover: cover ₹2,80,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹79,800 a year. Protection gap: ₹95,00,000.\n2. Lower cover: cover ₹1,85,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹52,725 a year. Protection gap: ₹1,90,00,000.\n3. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Waiver of premium, Critical illness. Indicative premium ₹95,625 a year. Protection gap: ₹0.\n4. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Accidental death benefit, Critical illness. Indicative premium ₹1,01,250 a year. Protection gap: ₹0.\n5. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium. Indicative premium ₹76,875 a year. Protection gap: ₹0.\n6. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: None. Indicative premium ₹60,000 a year. Protection gap: ₹0.\n7. Another premium-paying option: cover ₹3,75,00,000, Limited pay (10 years), riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹1,81,687.50 a year. Protection gap: ₹0.\n8. Another premium-paying option: cover ₹3,75,00,000, Single premium, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹9,61,875 as a single premium. Protection gap: ₹0."
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [
              "999A007V01",
              "999A008V01",
              "999A009V01"
            ],
            "sum_assured_inr": "28000000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 1"
      },
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [
              "999A007V01",
              "999A008V01",
              "999A009V01"
            ],
            "sum_assured_inr": "18500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 2"
      },
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [
              "999A008V01",
              "999A009V01"
            ],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 3"
      },
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [
              "999A007V01",
              "999A009V01"
            ],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 4"
      },
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [
              "999A007V01",
              "999A008V01"
            ],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 5"
      },
      {
        "action": {
          "payload": {
            "ppt": "regular",
            "rider_uins": [],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 6"
      },
      {
        "action": {
          "payload": {
            "ppt": "limited_10",
            "rider_uins": [
              "999A007V01",
              "999A008V01",
              "999A009V01"
            ],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 7"
      },
      {
        "action": {
          "payload": {
            "ppt": "single",
            "rider_uins": [
              "999A007V01",
              "999A008V01",
              "999A009V01"
            ],
            "sum_assured_inr": "37500000",
            "term_years": 26,
            "uin": "999N001V02"
          },
          "type": "APPLY"
        },
        "label": "Choose 8"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "Talk to a licensed advisor"
      }
    ],
    "sources": [],
    "text": "Here are lower-cost versions of Suraksha Term Shield (999N001V02), each with its protection gap against your recommended cover:\n1. Lower cover: cover ₹2,80,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹79,800 a year. Protection gap: ₹95,00,000.\n2. Lower cover: cover ₹1,85,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹52,725 a year. Protection gap: ₹1,90,00,000.\n3. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Waiver of premium, Critical illness. Indicative premium ₹95,625 a year. Protection gap: ₹0.\n4. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Accidental death benefit, Critical illness. Indicative premium ₹1,01,250 a year. Protection gap: ₹0.\n5. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: Accidental death benefit, Waiver of premium. Indicative premium ₹76,875 a year. Protection gap: ₹0.\n6. Fewer riders: cover ₹3,75,00,000, Regular pay, riders: None. Indicative premium ₹60,000 a year. Protection gap: ₹0.\n7. Another premium-paying option: cover ₹3,75,00,000, Limited pay (10 years), riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹1,81,687.50 a year. Protection gap: ₹0.\n8. Another premium-paying option: cover ₹3,75,00,000, Single premium, riders: Accidental death benefit, Waiver of premium, Critical illness. Indicative premium ₹9,61,875 as a single premium. Protection gap: ₹0."
  },
  "state": "S3",
  "turn_id": "01a12ec5-4ee8-7000-bd63-691f1107a85f"
};

/** APPLY {uin: 999N001V02} from CHOOSE's quick reply: the selection and the acknowledgment asked. */
export const SELECTION: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:selection_card",
        "text": "You chose Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying option: Regular pay\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0001, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0"
      },
      {
        "id": "template:ack_ask",
        "text": "Before I pass this on, please confirm that you have read the important information, the customer information sheet and the policy wording for Suraksha Term Shield (999N001V02) shown above."
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "disclosure_set_sha256": "fdcc70b9a658fda8ce69daf5fec4c0420de4a784911541b292a0e53e5450eee8",
            "document_sha256": {
              "CIS": "9b12ebd6afb8d049704ad02679a8bcd1d6bccc83435e38abf465e98776782eae",
              "POLICY_WORDING": "e97977657813a04229582b30bafa45347ffc4fd47c1a08ed46e9d3949af138ff"
            },
            "registry_version": "2026.09.1",
            "uin": "999N001V02"
          },
          "type": "DISCLOSURE_ACK"
        },
        "label": "I have read them"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "Talk to a licensed advisor"
      }
    ],
    "sources": [],
    "text": "You chose Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying option: Regular pay\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0001, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\n\nBefore I pass this on, please confirm that you have read the important information, the customer information sheet and the policy wording for Suraksha Term Shield (999N001V02) shown above."
  },
  "state": "S3",
  "turn_id": "01a12ec5-5c4b-7000-9179-534269ca44e5"
};

/** DISCLOSURE_ACK with a zero set hash (s3/ack-mismatch turn 3): refused, the options presented again. */
export const ACK_MISMATCH: TurnResponse = {
  "documents": [
    {
      "documents": {
        "CIS": "9b12ebd6afb8d049704ad02679a8bcd1d6bccc83435e38abf465e98776782eae",
        "POLICY_WORDING": "e97977657813a04229582b30bafa45347ffc4fd47c1a08ed46e9d3949af138ff"
      },
      "uin": "999N001V02"
    },
    {
      "documents": {
        "CIS": "30948ccef5b45066a6e24d0d37ab1dbae16ba478c9ec781dc9f859e4d216fd06",
        "POLICY_WORDING": "993670f893e2b73bada887441736ccdb954b4bfe0519dc2309c9042e831a4167"
      },
      "uin": "999N002V01"
    }
  ],
  "message": {
    "citations": [
      {
        "handle": "R1",
        "ref": "RANK-FIT-TERM"
      },
      {
        "handle": "E1",
        "ref": "regulatory:irdai-ppi-mc-2024:3:f51511"
      },
      {
        "handle": "E2",
        "ref": "product:999N002V01:pw-v2:3:d3a8cc"
      },
      {
        "handle": "E3",
        "ref": "product:999N001V02:pw-v2:3:411cde"
      },
      {
        "handle": "R2",
        "ref": "RANK-FIT-TERM_ROP"
      },
      {
        "handle": "R3",
        "ref": "FIT-01,FIT-02,FIT-03,MA-01,AFL-01,AFF-01"
      }
    ],
    "cta": null,
    "disclosures": [
      {
        "set_sha256": "fdcc70b9a658fda8ce69daf5fec4c0420de4a784911541b292a0e53e5450eee8",
        "uin": "999N001V02"
      },
      {
        "set_sha256": "49177622f860773b0b62c67217e0334904761baf9e3d95fe02a2366e4ff003c6",
        "uin": "999N002V01"
      }
    ],
    "form": null,
    "parts": [
      {
        "id": "template:ack_mismatch",
        "text": "What you confirmed is not the version I showed you, so here are the options and their information again. Please read them before you confirm."
      },
      {
        "id": "needs_recap",
        "text": "Your needs, as you confirmed them\nGoals: Protect your family's income, Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year."
      },
      {
        "id": "option_card:999N001V02",
        "text": "Option 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0011, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)"
      },
      {
        "id": "option_card:999N002V01",
        "text": "Option 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nIndicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0012, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)"
      },
      {
        "id": "comparison",
        "text": "How the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| Plan type | Term | Term with return of premium |\n| Entry age (years) | 18–65 | 18–55 |\n| Maximum age at maturity (years) | 85 | 75 |\n| Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| Policy term (years) | 10–40 | 15–35 |\n| Premium-paying options | Regular pay, Limited pay (10 years), Single premium | Regular pay, Limited pay (10 years) |\n| Benefit payment options | Lump sum, Monthly income | Lump sum |\n| Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |"
      },
      {
        "id": "why_it_fits",
        "text": "Option [Source: Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide."
      },
      {
        "id": "disclosures:999N001V02",
        "text": "Important information about Suraksha Term Shield (999N001V02)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change."
      },
      {
        "id": "disclosures:999N002V01",
        "text": "Important information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change."
      },
      {
        "id": "cta",
        "text": "What would you like to do next?\n- Apply for one of these plans\n- Talk to a licensed advisor\n- Change my answers\n- Save for later"
      },
      {
        "id": "sources",
        "text": "Sources\nIRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nSuraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nSuraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {},
          "type": "APPLY"
        },
        "label": "Apply for one of these plans"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "Talk to a licensed advisor"
      },
      {
        "action": {
          "payload": {},
          "type": "REVISE"
        },
        "label": "Change my answers"
      },
      {
        "action": {
          "payload": {},
          "type": "SAVE"
        },
        "label": "Save for later"
      }
    ],
    "sources": [
      {
        "effective_from": "2024-09-05",
        "section": "3. Customer Information Sheet",
        "title": "IRDAI › Master Circular on Protection of Policyholders' Interests 2024",
        "uri": "content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3",
        "version": "2024"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield ROP (999N002V01) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N002V01-policy-wording-v2.md#3",
        "version": "v2"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield (999N001V02) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N001V02-policy-wording-v2.md#3",
        "version": "v2"
      }
    ],
    "text": "What you confirmed is not the version I showed you, so here are the options and their information again. Please read them before you confirm.\n\nYour needs, as you confirmed them\nGoals: Protect your family's income, Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year.\n\nOption 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nIndicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0011, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)\n\nOption 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nIndicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0012, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)\n\nHow the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| Plan type | Term | Term with return of premium |\n| Entry age (years) | 18–65 | 18–55 |\n| Maximum age at maturity (years) | 85 | 75 |\n| Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| Policy term (years) | 10–40 | 15–35 |\n| Premium-paying options | Regular pay, Limited pay (10 years), Single premium | Regular pay, Limited pay (10 years) |\n| Benefit payment options | Lump sum, Monthly income | Lump sum |\n| Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |\n\nOption [Source: Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nImportant information about Suraksha Term Shield (999N001V02)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change.\n\nImportant information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: Approved solicitation statement.\nDUMMY: Premium is indicative, final after underwriting.\nDUMMY: Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: Consequences of misstatement or non-disclosure.\nDUMMY: 30-day free-look period.\nDUMMY: Tax treatment depends on the law in force and can change.\n\nWhat would you like to do next?\n- Apply for one of these plans\n- Talk to a licensed advisor\n- Change my answers\n- Save for later\n\nSources\nIRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nSuraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nSuraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
  },
  "state": "S3",
  "turn_id": "01a12ec5-695b-7000-8bff-2ad8f9f8f116"
};

/** LANGUAGE hi-IN in S3 (matrix/language-switch__s3): the recommendation again with the hi-IN sets. */
export const S3_HI: TurnResponse = {
  "documents": [
    {
      "documents": {
        "CIS": "9b12ebd6afb8d049704ad02679a8bcd1d6bccc83435e38abf465e98776782eae",
        "POLICY_WORDING": "e97977657813a04229582b30bafa45347ffc4fd47c1a08ed46e9d3949af138ff"
      },
      "uin": "999N001V02"
    },
    {
      "documents": {
        "CIS": "30948ccef5b45066a6e24d0d37ab1dbae16ba478c9ec781dc9f859e4d216fd06",
        "POLICY_WORDING": "993670f893e2b73bada887441736ccdb954b4bfe0519dc2309c9042e831a4167"
      },
      "uin": "999N002V01"
    }
  ],
  "message": {
    "citations": [
      {
        "handle": "R1",
        "ref": "RANK-FIT-TERM"
      },
      {
        "handle": "E1",
        "ref": "regulatory:irdai-ppi-mc-2024:3:f51511"
      },
      {
        "handle": "E2",
        "ref": "product:999N002V01:pw-v2:3:d3a8cc"
      },
      {
        "handle": "E3",
        "ref": "product:999N001V02:pw-v2:3:411cde"
      },
      {
        "handle": "R2",
        "ref": "RANK-FIT-TERM_ROP"
      },
      {
        "handle": "R3",
        "ref": "FIT-01,FIT-02,FIT-03,MA-01,AFL-01,AFF-01"
      }
    ],
    "cta": null,
    "disclosures": [
      {
        "set_sha256": "a3ef460ed8b2e48eeb4c9352b8e6ae7eadab5154cffb50f3950680e98531fbc8",
        "uin": "999N001V02"
      },
      {
        "set_sha256": "8181f05a5905420c6f1a6e4a31abe4c1088583c28cc672864c728abdbfc17167",
        "uin": "999N002V01"
      }
    ],
    "form": null,
    "parts": [
      {
        "id": "needs_recap",
        "text": "DUMMY: [hi translation pending approval] Your needs, as you confirmed them\nGoals: DUMMY: [hi translation pending approval] Protect your family's income, DUMMY: [hi translation pending approval] Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year."
      },
      {
        "id": "option_card:999N001V02",
        "text": "DUMMY: [hi translation pending approval] Option 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nDUMMY: [hi translation pending approval] Indicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0013, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)"
      },
      {
        "id": "option_card:999N002V01",
        "text": "DUMMY: [hi translation pending approval] Option 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nDUMMY: [hi translation pending approval] Indicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0014, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)"
      },
      {
        "id": "comparison",
        "text": "DUMMY: [hi translation pending approval] How the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| DUMMY: [hi translation pending approval] Plan type | DUMMY: [hi translation pending approval] Term | DUMMY: [hi translation pending approval] Term with return of premium |\n| DUMMY: [hi translation pending approval] Entry age (years) | 18–65 | 18–55 |\n| DUMMY: [hi translation pending approval] Maximum age at maturity (years) | 85 | 75 |\n| DUMMY: [hi translation pending approval] Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| DUMMY: [hi translation pending approval] Policy term (years) | 10–40 | 15–35 |\n| DUMMY: [hi translation pending approval] Premium-paying options | DUMMY: [hi translation pending approval] Regular pay, DUMMY: [hi translation pending approval] Limited pay (10 years), DUMMY: [hi translation pending approval] Single premium | DUMMY: [hi translation pending approval] Regular pay, DUMMY: [hi translation pending approval] Limited pay (10 years) |\n| DUMMY: [hi translation pending approval] Benefit payment options | DUMMY: [hi translation pending approval] Lump sum, DUMMY: [hi translation pending approval] Monthly income | DUMMY: [hi translation pending approval] Lump sum |\n| DUMMY: [hi translation pending approval] Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |"
      },
      {
        "id": "why_it_fits",
        "text": "Option [Source: DUMMY: [hi translation pending approval] Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: DUMMY: [hi translation pending approval] Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: DUMMY: [hi translation pending approval] Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide."
      },
      {
        "id": "disclosures:999N001V02",
        "text": "DUMMY: [hi translation pending approval] Important information about Suraksha Term Shield (999N001V02)\nDUMMY: [hi translation pending approval] Approved solicitation statement.\nDUMMY: [hi translation pending approval] Premium is indicative, final after underwriting.\nDUMMY: [hi translation pending approval] Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: [hi translation pending approval] Consequences of misstatement or non-disclosure.\nDUMMY: [hi translation pending approval] 30-day free-look period.\nDUMMY: [hi translation pending approval] Tax treatment depends on the law in force and can change."
      },
      {
        "id": "disclosures:999N002V01",
        "text": "DUMMY: [hi translation pending approval] Important information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: [hi translation pending approval] Approved solicitation statement.\nDUMMY: [hi translation pending approval] Premium is indicative, final after underwriting.\nDUMMY: [hi translation pending approval] Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: [hi translation pending approval] Consequences of misstatement or non-disclosure.\nDUMMY: [hi translation pending approval] 30-day free-look period.\nDUMMY: [hi translation pending approval] Tax treatment depends on the law in force and can change."
      },
      {
        "id": "cta",
        "text": "DUMMY: [hi translation pending approval] What would you like to do next?\n- DUMMY: [hi translation pending approval] Apply for one of these plans\n- DUMMY: [hi translation pending approval] Talk to a licensed advisor\n- DUMMY: [hi translation pending approval] Change my answers\n- DUMMY: [hi translation pending approval] Save for later"
      },
      {
        "id": "sources",
        "text": "DUMMY: [hi translation pending approval] Sources\nDUMMY: [hi translation pending approval] IRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nDUMMY: [hi translation pending approval] Suraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nDUMMY: [hi translation pending approval] Suraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {},
          "type": "APPLY"
        },
        "label": "DUMMY: [hi translation pending approval] Apply for one of these plans"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "DUMMY: [hi translation pending approval] Talk to a licensed advisor"
      },
      {
        "action": {
          "payload": {},
          "type": "REVISE"
        },
        "label": "DUMMY: [hi translation pending approval] Change my answers"
      },
      {
        "action": {
          "payload": {},
          "type": "SAVE"
        },
        "label": "DUMMY: [hi translation pending approval] Save for later"
      }
    ],
    "sources": [
      {
        "effective_from": "2024-09-05",
        "section": "3. Customer Information Sheet",
        "title": "IRDAI › Master Circular on Protection of Policyholders' Interests 2024",
        "uri": "content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3",
        "version": "2024"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield ROP (999N002V01) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N002V01-policy-wording-v2.md#3",
        "version": "v2"
      },
      {
        "effective_from": "2026-09-01",
        "section": "3. Exclusions and waiting periods",
        "title": "Suraksha Term Shield (999N001V02) › Policy Wording v2",
        "uri": "content/seed/kb/product/999N001V02-policy-wording-v2.md#3",
        "version": "v2"
      }
    ],
    "text": "DUMMY: [hi translation pending approval] Your needs, as you confirmed them\nGoals: DUMMY: [hi translation pending approval] Protect your family's income, DUMMY: [hi translation pending approval] Repay loans\nCover you may need: ₹3,71,47,714.43. Recommended cover: ₹3,75,00,000 for 26 years.\nAssumptions: cover to age 60, 26 years of dependency, existing cover counted ₹7,50,000, income growth of 5% a year and a discount rate of 7% a year.\n\nDUMMY: [hi translation pending approval] Option 1: Suraksha Term Shield (999N001V02)\nCover: ₹3,75,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit, Waiver of premium, Critical illness\nDUMMY: [hi translation pending approval] Indicative premium: ₹1,06,875 a year, including GST. Quote Q-2026-10-12-0013, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹0\nDocuments: DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Customer information sheet v2 (content/seed/kb/product/999N001V02-cis-v2.md); DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Policy wording v2 (content/seed/kb/product/999N001V02-policy-wording-v2.md)\n\nDUMMY: [hi translation pending approval] Option 2: Suraksha Term Shield ROP (999N002V01)\nCover: ₹2,00,00,000\nPolicy term: 26 years\nPremium-paying term: 26 years\nRiders: Accidental death benefit\nDUMMY: [hi translation pending approval] Indicative premium: ₹90,000 a year, including GST. Quote Q-2026-10-12-0014, valid until 11 Nov 2026. The final premium is set after underwriting.\nProtection gap against your recommended cover: ₹1,75,00,000\nDocuments: DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Customer information sheet v2 (content/seed/kb/product/999N002V01-cis-v2.md); DUMMY: [hi translation pending approval] DUMMY: [hi translation pending approval] Policy wording v2 (content/seed/kb/product/999N002V01-policy-wording-v2.md)\n\nDUMMY: [hi translation pending approval] How the options compare\n| | Suraksha Term Shield (999N001V02) | Suraksha Term Shield ROP (999N002V01) |\n|---|---|---|\n| DUMMY: [hi translation pending approval] Plan type | DUMMY: [hi translation pending approval] Term | DUMMY: [hi translation pending approval] Term with return of premium |\n| DUMMY: [hi translation pending approval] Entry age (years) | 18–65 | 18–55 |\n| DUMMY: [hi translation pending approval] Maximum age at maturity (years) | 85 | 75 |\n| DUMMY: [hi translation pending approval] Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |\n| DUMMY: [hi translation pending approval] Policy term (years) | 10–40 | 15–35 |\n| DUMMY: [hi translation pending approval] Premium-paying options | DUMMY: [hi translation pending approval] Regular pay, DUMMY: [hi translation pending approval] Limited pay (10 years), DUMMY: [hi translation pending approval] Single premium | DUMMY: [hi translation pending approval] Regular pay, DUMMY: [hi translation pending approval] Limited pay (10 years) |\n| DUMMY: [hi translation pending approval] Benefit payment options | DUMMY: [hi translation pending approval] Lump sum, DUMMY: [hi translation pending approval] Monthly income | DUMMY: [hi translation pending approval] Lump sum |\n| DUMMY: [hi translation pending approval] Riders available | Accidental death benefit, Waiver of premium, Critical illness | Accidental death benefit |\n\nOption [Source: DUMMY: [hi translation pending approval] Our ranking of Suraksha Term Shield (999N001V02)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: DUMMY: [hi translation pending approval] Our ranking of Suraksha Term Shield ROP (999N002V01)] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nOption [Source: DUMMY: [hi translation pending approval] Your needs assessment] fits the protection need you described and stays within the budget you shared. Its main terms and exclusions are in the cited documents [Source: PPI_MasterCircular_2024 §3] [Source: SurakshaTermShieldROP_999N002V01_PolicyWording §3] [Source: SurakshaTermShield_999N001V02_PolicyWording §3]. Please read its disclosures before you decide.\n\nDUMMY: [hi translation pending approval] Important information about Suraksha Term Shield (999N001V02)\nDUMMY: [hi translation pending approval] Approved solicitation statement.\nDUMMY: [hi translation pending approval] Premium is indicative, final after underwriting.\nDUMMY: [hi translation pending approval] Key exclusions and waiting periods summary for 999N001V02.\nDUMMY: [hi translation pending approval] Consequences of misstatement or non-disclosure.\nDUMMY: [hi translation pending approval] 30-day free-look period.\nDUMMY: [hi translation pending approval] Tax treatment depends on the law in force and can change.\n\nDUMMY: [hi translation pending approval] Important information about Suraksha Term Shield ROP (999N002V01)\nDUMMY: [hi translation pending approval] Approved solicitation statement.\nDUMMY: [hi translation pending approval] Premium is indicative, final after underwriting.\nDUMMY: [hi translation pending approval] Key exclusions and waiting periods summary for 999N002V01.\nDUMMY: [hi translation pending approval] Consequences of misstatement or non-disclosure.\nDUMMY: [hi translation pending approval] 30-day free-look period.\nDUMMY: [hi translation pending approval] Tax treatment depends on the law in force and can change.\n\nDUMMY: [hi translation pending approval] What would you like to do next?\n- DUMMY: [hi translation pending approval] Apply for one of these plans\n- DUMMY: [hi translation pending approval] Talk to a licensed advisor\n- DUMMY: [hi translation pending approval] Change my answers\n- DUMMY: [hi translation pending approval] Save for later\n\nDUMMY: [hi translation pending approval] Sources\nDUMMY: [hi translation pending approval] IRDAI › Master Circular on Protection of Policyholders' Interests 2024, 3. Customer Information Sheet, version 2024, effective 5 Sep 2024: content/seed/kb/regulatory/irdai-ppi-master-circular-2024.md#3\nDUMMY: [hi translation pending approval] Suraksha Term Shield ROP (999N002V01) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N002V01-policy-wording-v2.md#3\nDUMMY: [hi translation pending approval] Suraksha Term Shield (999N001V02) › Policy Wording v2, 3. Exclusions and waiting periods, version v2, effective 1 Sep 2026: content/seed/kb/product/999N001V02-policy-wording-v2.md#3"
  },
  "state": "S3",
  "turn_id": "01a12ec5-7b96-7000-a9fb-aac90965142b"
};

/** DISCLOSURE_ACK as offered, after APPLY {uin}: the signed intake taken (S3.4). */
export const HANDOFF: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:handoff_application",
        "text": "DUMMY: Approved hand-off text pending. Your application for Suraksha Term Shield (999N001V02) has been passed to our application team with reference INT-000001. They will contact you to complete it."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "DUMMY: Approved hand-off text pending. Your application for Suraksha Term Shield (999N001V02) has been passed to our application team with reference INT-000001. They will contact you to complete it."
  },
  "state": "HANDOFF",
  "turn_id": "01a12ec5-b9d8-7000-a5f9-53f0b8437004"
};

/** A tax question in S3 (s3/tax turn 2, the same stub scripts): the cited answer and its sources. */
export const TAX_ANSWER: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [
      {
        "handle": "E2",
        "ref": "tax:counsel-note-2026:4:25d8b6"
      }
    ],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "generated:answer",
        "text": "Premiums can qualify for a deduction only under the old tax regime, subject to conditions [Source: SurakshaLife_TaxCounselNote_2026 §4]."
      },
      {
        "id": "template:side_query_caveat",
        "text": "This is general information; the policy wording decides."
      },
      {
        "id": "template:tax_condition",
        "text": "DUMMY: Approved text pending. Whether a tax benefit applies to you depends on the tax regime you choose and your other policies, so I can't work out your tax. Please check with a tax professional."
      },
      {
        "id": "template:regime_ask",
        "text": "Tax benefits depend on your tax regime. Are you on the old regime or the new one? I use this only to choose which rules to explain, never to work out your tax."
      },
      {
        "id": "registry:DISC-GLOBAL-TAX-05",
        "text": "DUMMY: Tax treatment depends on the law in force and can change."
      },
      {
        "id": "template:sources",
        "text": "Sources\nSuraksha Life (DUMMY insurer) › Tax counsel note on life insurance 2026, 4. What the assistant may and may not say about tax, version 2026.1, effective 1 Sep 2026: content/seed/kb/tax/tax-counsel-note-life-insurance.md#4"
      },
      {
        "id": "template:side_query_bridge",
        "text": "Coming back to where we were:"
      },
      {
        "id": "template:s3_choices",
        "text": "You can choose one of the options above, ask me about them, or pick one of these:"
      }
    ],
    "quick_replies": [
      {
        "action": {
          "payload": {
            "confirmed": true,
            "slot": "tax_regime",
            "value": "old"
          },
          "type": "FACT"
        },
        "label": "old tax regime"
      },
      {
        "action": {
          "payload": {
            "confirmed": true,
            "slot": "tax_regime",
            "value": "new"
          },
          "type": "FACT"
        },
        "label": "new tax regime"
      },
      {
        "action": {
          "payload": {},
          "type": "APPLY"
        },
        "label": "Apply for one of these plans"
      },
      {
        "action": {
          "payload": {},
          "type": "HUMAN_REQUEST"
        },
        "label": "Talk to a licensed advisor"
      },
      {
        "action": {
          "payload": {},
          "type": "REVISE"
        },
        "label": "Change my answers"
      },
      {
        "action": {
          "payload": {},
          "type": "SAVE"
        },
        "label": "Save for later"
      }
    ],
    "sources": [
      {
        "effective_from": "2026-09-01",
        "section": "4. What the assistant may and may not say about tax",
        "title": "Suraksha Life (DUMMY insurer) › Tax counsel note on life insurance 2026",
        "uri": "content/seed/kb/tax/tax-counsel-note-life-insurance.md#4",
        "version": "2026.1"
      }
    ],
    "text": "Premiums can qualify for a deduction only under the old tax regime, subject to conditions [Source: SurakshaLife_TaxCounselNote_2026 §4].\n\nThis is general information; the policy wording decides.\n\nDUMMY: Approved text pending. Whether a tax benefit applies to you depends on the tax regime you choose and your other policies, so I can't work out your tax. Please check with a tax professional.\n\nTax benefits depend on your tax regime. Are you on the old regime or the new one? I use this only to choose which rules to explain, never to work out your tax.\n\nDUMMY: Tax treatment depends on the law in force and can change.\n\nSources\nSuraksha Life (DUMMY insurer) › Tax counsel note on life insurance 2026, 4. What the assistant may and may not say about tax, version 2026.1, effective 1 Sep 2026: content/seed/kb/tax/tax-counsel-note-life-insurance.md#4\n\nComing back to where we were:\n\nYou can choose one of the options above, ask me about them, or pick one of these:"
  },
  "state": "S3",
  "turn_id": "01a12ec7-3670-7000-883f-bb9fe7407fa8"
};

/** DISCLOSURE_ACK with the stub journey failing (s3/journey-down turn 3): S3 stays, an advisor (P2). */
export const JOURNEY_DOWN: TurnResponse = {
  "documents": [],
  "message": {
    "citations": [],
    "cta": null,
    "disclosures": [],
    "form": null,
    "parts": [
      {
        "id": "template:journey_down",
        "text": "DUMMY: Approved text pending. Your choice and your answers are saved, but I couldn't pass your application on just now. A licensed advisor will complete it with you."
      },
      {
        "id": "template:handoff",
        "text": "DUMMY: Approved hand-off script pending. A licensed advisor will continue this conversation."
      }
    ],
    "quick_replies": [],
    "sources": [],
    "text": "DUMMY: Approved text pending. Your choice and your answers are saved, but I couldn't pass your application on just now. A licensed advisor will complete it with you.\n\nDUMMY: Approved hand-off script pending. A licensed advisor will continue this conversation."
  },
  "state": "S3",
  "turn_id": "01a12ec7-544b-7000-bc32-6088a5d35a35"
};
