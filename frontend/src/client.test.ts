import { describe, expect, it } from "vitest";
import { ApiError, CLOSED_STATES, createSession, deleteSession, readEvents, sendTurn } from "./client";
import { created, fakeApi, json, problem, turn } from "./test-support";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const START = { action: { type: "START", payload: {} } } as const;

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => undefined,
    (e: unknown) => e,
  );
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

describe("createSession", () => {
  it("opens a web session in the given locale and keeps its token", async () => {
    const api = fakeApi();
    const session = await createSession("hi-IN");
    expect(api.calls).toEqual([
      expect.objectContaining({ method: "POST", url: "/v1/sessions", body: { channel: "web", locale: "hi-IN" } }),
    ]);
    expect(api.calls[0]?.headers.authorization).toBeUndefined();
    const c = created(1);
    expect(session).toEqual({ id: c.session_id, token: c.session_token, eventsUrl: c.events_url });
  });

  it.each([
    ["no token", { ...created(1), session_token: 7 }],
    ["an events url elsewhere", { ...created(1), events_url: "https://elsewhere.example/v1/sessions/x/events" }],
    ["a session id that is not a uuid", { ...created(1), session_id: "../../internal" }],
  ])("refuses a created session with %s as malformed", async (_, body) => {
    fakeApi().onCreate(json(201, body));
    expect((await failure(createSession("en-IN"))).kind).toBe("malformed");
  });
});

describe("sendTurn and deleteSession", () => {
  it("sends the body exactly as given, with the bearer token and the given key", async () => {
    const api = fakeApi();
    const session = await createSession("en-IN");
    const action = { type: "DISCLOSURE_ACK", payload: { uin: "999N001V02", registry_version: "2026.09.1", disclosure_set_sha256: "a".repeat(64), document_sha256: { CIS: "b".repeat(64) } } } as const;
    api.answer(json(200, turn("S3", ["ok"])), json(200, turn("S3", ["ok"])));
    const key = crypto.randomUUID();
    await sendTurn(session, { action }, key);
    await sendTurn(session, { action }, key);
    const [first, second] = api.turns();
    expect(first).toMatchObject({ method: "POST", url: `/v1/sessions/${session.id}/turns`, body: { action } });
    expect(first?.headers.authorization).toBe(`Bearer ${session.token}`);
    expect(first?.headers["idempotency-key"]).toBe(key);
    expect(second?.headers["idempotency-key"]).toBe(key);
  });

  it("returns the released turn", async () => {
    const api = fakeApi();
    const session = await createSession("en-IN");
    api.answer(json(200, turn("S0", ["Hello", "There"])));
    const released = await sendTurn(session, START, crypto.randomUUID());
    expect(released.message.parts.map((p) => p.text)).toEqual(["Hello", "There"]);
  });

  it("deletes the session with its token and a fresh key", async () => {
    const api = fakeApi();
    const session = await createSession("en-IN");
    api.answer(json(200, turn("DATA_ERASURE", ["Erased"])));
    const released = await deleteSession(session);
    expect(released.state).toBe("DATA_ERASURE");
    const call = api.calls.at(-1);
    expect(call).toMatchObject({ method: "DELETE", url: `/v1/sessions/${session.id}`, body: undefined });
    expect(call?.headers.authorization).toBe(`Bearer ${session.token}`);
    expect(call?.headers["idempotency-key"]).toMatch(UUID);
  });
});

describe("failures", () => {
  async function turnFailing(answer: Response | Error): Promise<ApiError> {
    const api = fakeApi();
    const session = await createSession("en-IN");
    api.answer(answer);
    return failure(sendTurn(session, START, crypto.randomUUID()));
  }

  it("a problem+json answer is a problem with its status and code", async () => {
    const error = await turnFailing(problem(429, "RATE_LIMITED"));
    expect([error.kind, error.status, error.code]).toEqual(["problem", 429, "RATE_LIMITED"]);
  });

  it("a network failure is unreachable", async () => {
    expect((await turnFailing(new TypeError("Failed to fetch"))).kind).toBe("unreachable");
  });

  it("an error that is not the API's (the dev proxy's empty 500) is unreachable", async () => {
    const error = await turnFailing(new Response("", { status: 500, headers: { "content-type": "text/plain" } }));
    expect([error.kind, error.status]).toEqual(["unreachable", 500]);
  });

  it.each([
    ["not JSON", new Response("<html>", { status: 200 })],
    ["no message", json(200, { state: "S0" })],
    ["a part without text", json(200, { ...turn("S0", []), message: { ...turn("S0", []).message, parts: [{ id: "x" }] } })],
    ["a quick reply without an action", json(200, turn("S0", ["a"], [{ label: "x" } as never]))],
  ])("a 200 with %s is malformed", async (_, answer) => {
    expect((await turnFailing(answer)).kind).toBe("malformed");
  });

  it("an error's message names its kind only, never the body", async () => {
    const error = await turnFailing(json(200, { secret: "customer words" }));
    expect(error.message).toBe("malformed");
  });
});

describe("readEvents", () => {
  it("reads named JSON events across chunks, skips pings, and sends the token in a header only", async () => {
    const api = fakeApi();
    const session = await createSession("en-IN");
    const seen: [string, unknown][] = [];
    const controller = new AbortController();
    const done = readEvents(session, (name, data) => seen.push([name, data]), controller.signal);
    await expect.poll(() => api.calls.length).toBe(2);
    api.push(": ping\n\n");
    api.push('event: turn.status\ndata: {"turn_id": "t1", ');
    api.push('"status": "analysing"}\n\nevent: turn.status\ndata: {"turn_id":');
    api.push('\ndata: "t1", "status": "composing"}\n\nevent: x\ndata: not json\n\n');
    await expect.poll(() => seen.length).toBe(2);
    expect(seen).toEqual([
      ["turn.status", { turn_id: "t1", status: "analysing" }],
      ["turn.status", { turn_id: "t1", status: "composing" }],
    ]);
    const call = api.calls[1];
    expect(call?.url).toBe(session.eventsUrl);
    expect(call?.headers.authorization).toBe(`Bearer ${session.token}`);
    controller.abort();
    await expect(done).rejects.toBeDefined();
  });

  it("never puts the token in a URL", async () => {
    const api = fakeApi();
    const session = await createSession("en-IN");
    api.answer(json(200, turn("S0", ["a"])), json(200, turn("S0", ["a"])));
    await sendTurn(session, START, crypto.randomUUID());
    await deleteSession(session);
    const controller = new AbortController();
    const reading = readEvents(session, () => {}, controller.signal);
    await expect.poll(() => api.calls.length).toBe(4);
    controller.abort();
    await reading.catch(() => {});
    expect(api.calls.map((c) => c.url).filter((url) => url.includes(session.token))).toEqual([]);
  });
});

it("the closed states are the spec's", () => {
  expect([...CLOSED_STATES].sort()).toEqual(["DATA_ERASURE", "EXIT", "EXIT_ADVISORY", "HANDOFF", "HUMAN_ESCALATION"]);
});
