// The Conversation API client, over the types generated from contracts/openapi/
// conversation-api.v1.yaml (make ui-types). The session token is only ever an Authorization
// header: never in a URL, storage or a log.
import type { components } from "./conversation-api";

type Schemas = components["schemas"];
export type FsmState = Schemas["FsmState"];
export type Locale = Schemas["Locale"];
export type QuickReply = Schemas["QuickReply"];
export type TurnRequest = Schemas["TurnRequest"];
export type TurnResponse = Schemas["TurnResponse"];

export interface Session {
  id: string;
  token: string;
  eventsUrl: string;
}

/** The closed states (the spec's FsmState): the session takes no further journey. */
export const CLOSED_STATES: ReadonlySet<FsmState> = new Set<FsmState>([
  "HUMAN_ESCALATION",
  "EXIT",
  "EXIT_ADVISORY",
  "DATA_ERASURE",
  "HANDOFF",
]);

/**
 * `problem`: the API's problem+json (`status`, `code`). `unreachable`: no answer from the API (a
 * network failure, or an error that is not a problem, such as the dev proxy's when the backend is
 * down). `malformed`: a success whose body breaks the contract. The message is the kind only.
 */
export class ApiError extends Error {
  readonly kind: "problem" | "unreachable" | "malformed";
  readonly status: number;
  readonly code: string;

  constructor(kind: ApiError["kind"], status = 0, code = "") {
    super(kind);
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

const SESSION_ID = /^[0-9a-f-]{36}$/;

function auth(session: Session): Record<string, string> {
  return { Authorization: `Bearer ${session.token}` };
}

async function call(url: string, init: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError("unreachable");
  }
  const body: unknown = await response.json().catch(() => undefined);
  if (response.ok) {
    if (body === undefined) throw new ApiError("malformed", response.status);
    return body;
  }
  const code = (body as { code?: unknown } | null | undefined)?.code;
  const isProblem = response.headers.get("content-type")?.startsWith("application/problem+json");
  if (isProblem && typeof code === "string") throw new ApiError("problem", response.status, code);
  throw new ApiError("unreachable", response.status);
}

/** The released turn, if the body has what the view reads (the server side proves the rest). */
function released(body: unknown): TurnResponse {
  const turn = body as TurnResponse | null;
  const message = turn?.message;
  const ok =
    typeof turn?.state === "string" &&
    typeof message?.text === "string" &&
    Array.isArray(message.parts) &&
    message.parts.every((p) => typeof p?.id === "string" && typeof p.text === "string") &&
    Array.isArray(message.quick_replies) &&
    message.quick_replies.every((q) => typeof q?.label === "string" && typeof q.action?.type === "string");
  if (!turn || !ok) throw new ApiError("malformed", 200);
  return turn;
}

export async function createSession(locale: Locale): Promise<Session> {
  const request: Schemas["CreateSessionRequest"] = { channel: "web", locale };
  const body = await call("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const s = body as Partial<Schemas["CreatedSession"]> | null;
  const id = s?.session_id;
  // The events URL must be this session's, on this origin, so the token goes nowhere else.
  if (
    typeof id !== "string" ||
    !SESSION_ID.test(id) ||
    typeof s?.session_token !== "string" ||
    s.events_url !== `/v1/sessions/${id}/events`
  ) {
    throw new ApiError("malformed", 201);
  }
  return { id, token: s.session_token, eventsUrl: s.events_url };
}

/** One turn. `key` is a fresh UUID per turn, and the same one only on an explicit retry. */
export async function sendTurn(session: Session, request: TurnRequest, key: string): Promise<TurnResponse> {
  const body = await call(`/v1/sessions/${session.id}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key, ...auth(session) },
    body: JSON.stringify(request),
  });
  return released(body);
}

export async function deleteSession(session: Session, key: string = crypto.randomUUID()): Promise<TurnResponse> {
  const body = await call(`/v1/sessions/${session.id}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": key, ...auth(session) },
  });
  return released(body);
}

/**
 * The session's server-sent events, read with fetch because EventSource cannot send the
 * Authorization header. Calls `onEvent(name, data)` per JSON event until `signal` aborts (the
 * promise then rejects) or the stream ends.
 */
export async function readEvents(
  session: Session,
  onEvent: (name: string, data: unknown) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(session.eventsUrl, { headers: auth(session), signal });
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    // ponytail: LF framing, as FastAPI's fastapi.sse sends it; CR/CRLF if another server answers.
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      let name = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") name = text;
        else if (field === "data") data.push(text);
      }
      if (!data.length) continue; // a comment, such as the keep-alive ": ping"
      let parsed: unknown;
      try {
        parsed = JSON.parse(data.join("\n"));
      } catch {
        continue;
      }
      onEvent(name, parsed);
    }
  }
}
