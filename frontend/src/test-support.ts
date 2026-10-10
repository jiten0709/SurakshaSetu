import { vi } from "vitest";
import type { components } from "./conversation-api";

type Schemas = components["schemas"];

/** The n-th session the fake API creates (1-based); the token is a sentinel for leak checks. */
export function created(n: number): Schemas["CreatedSession"] {
  const id = `0192a5b0-0000-7000-8000-${String(n).padStart(12, "0")}`;
  return { session_id: id, session_token: `tok-SENTINEL-${n}`, events_url: `/v1/sessions/${id}/events` };
}

/** A released turn whose parts are the given texts (synthetic, never legal text). */
export function turn(
  state: Schemas["FsmState"],
  texts: string[],
  quickReplies: Schemas["QuickReply"][] = [],
): Schemas["TurnResponse"] {
  return {
    turn_id: "0192a5b0-0000-7000-8000-0000000000aa",
    state,
    documents: [],
    message: {
      text: texts.join("\n\n"),
      parts: texts.map((text, i) => ({ id: `template:part_${i}`, text })),
      citations: [],
      sources: [],
      disclosures: [],
      cta: null,
      form: null,
      quick_replies: quickReplies,
    },
  };
}

export function json(status: number, body: unknown, type = "application/json"): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

export function problem(status: number, code: Schemas["Problem"]["code"]): Response {
  return json(status, { type: "about:blank", title: "x", status, code }, "application/problem+json");
}

export function deferred(): { promise: Promise<Response>; resolve: (r: Response) => void } {
  let resolve: (r: Response) => void = () => {};
  const promise = new Promise<Response>((r) => (resolve = r));
  return { promise, resolve };
}

export interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

type Answer = Response | Promise<Response> | Error;

/**
 * A fake Conversation API behind a mocked `fetch`: POST /v1/sessions answers `created(n)` (or a
 * queued `onCreate` answer), GET …/events an open event stream that `emit` and `push` write to,
 * and every other request the next queued `answer`. Each call is recorded (header names lower-case).
 */
export function fakeApi() {
  const calls: Call[] = [];
  const answers: Answer[] = [];
  const creates: Answer[] = [];
  let sessions = 0;
  let push: (chunk: string) => void = () => {};

  function stream(signal?: AbortSignal | null): Response {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = (chunk) => controller.enqueue(encoder.encode(chunk));
        signal?.addEventListener("abort", () => controller.error(signal.reason));
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  }

  async function next(queue: Answer[]): Promise<Response> {
    const answer = queue.shift();
    if (answer === undefined) throw new Error("fake API: nothing queued");
    if (answer instanceof Error) throw answer;
    return answer;
  }

  const fetch = vi.fn(async (url: string, init: RequestInit = {}) => {
    const headers = Object.fromEntries(new Headers(init.headers).entries());
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method: init.method ?? "GET", url, headers, body });
    if (url === "/v1/sessions") return creates.length ? next(creates) : json(201, created(++sessions));
    if (url.endsWith("/events")) return stream(init.signal);
    return next(answers);
  });
  vi.stubGlobal("fetch", fetch);

  return {
    calls,
    turns: () => calls.filter((c) => c.url.endsWith("/turns")),
    answer: (...a: Answer[]) => answers.push(...a),
    onCreate: (...a: Answer[]) => creates.push(...a),
    push: (chunk: string) => push(chunk),
    emit: (event: string, data: unknown) => push(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`),
  };
}
