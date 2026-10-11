// Released turns captured from the running API (make serve, pb-2026.10.8, the seed notices) on the
// s0 goldens' own turns, unchanged. Test-only: the app never imports this file. `satisfies` makes
// tsc check each body against the generated contract types.
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
