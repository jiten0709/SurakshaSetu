import { useEffect, useEffectEvent, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  CLOSED_STATES,
  createSession,
  readEvents,
  sendTurn,
  type Locale,
  type QuickReply,
  type Session,
  type TurnRequest,
  type TurnResponse,
} from "./client";

// The chat screen: a thin client. Everything it shows comes from a released turn or a quick reply
// it was given, and everything it sends is typed text or a quick reply's own action.

type Entry = { from: "you"; text: string } | { from: "server"; turn: TurnResponse };

interface Trouble {
  message: string;
  retry?: boolean;
  restart?: boolean;
}

const START: TurnRequest = { action: { type: "START", payload: {} } };

const STATUS: Record<string, string> = {
  analysing: "Reading your message…",
  composing: "Writing a reply…",
  validating: "Checking the reply…",
};

const UNREACHABLE = "We couldn't reach the service. Nothing was lost: press Retry.";
const GENERIC = "Something went wrong.";

/** What to tell the customer, and what they can do, per failure (the API's problem codes). */
function trouble(error: unknown): Trouble {
  if (!(error instanceof ApiError) || error.kind === "malformed") {
    return { message: `${GENERIC} Please start a new conversation.`, restart: true };
  }
  if (error.kind === "unreachable") return { message: UNREACHABLE, retry: true };
  switch (error.code) {
    case "RATE_LIMITED":
      return { message: "You're sending messages faster than we can answer. Please wait a minute, then press Retry.", retry: true };
    case "SESSION_BUSY":
      return { message: "Your previous message is still being answered. Press Retry in a moment.", retry: true };
    case "SESSION_EXPIRED":
      return { message: "This conversation has expired. Please start a new one.", restart: true };
    case "UNAUTHORIZED":
      return { message: "This conversation is no longer available. Please start a new one.", restart: true };
    case "SERVICE_UNAVAILABLE":
      return { message: UNREACHABLE, retry: true };
    case "REQUEST_ENTITY_TOO_LARGE":
      return { message: "That message is too long. Please shorten it." };
    default:
      return { message: `${GENERIC} Please press Retry.`, retry: true };
  }
}

export function Chat({ locale = "en-IN", busyRetryMs = 1000 }: { locale?: Locale; busyRetryMs?: number }) {
  const session = useRef<Session | null>(null); // the token lives here, in memory only
  const stream = useRef<AbortController | null>(null);
  const inFlight = useRef(false); // synchronous, so a double click sends one turn
  const pending = useRef<{ request: TurnRequest; key: string } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const restartButton = useRef<HTMLButtonElement>(null);
  const newest = useRef<HTMLLIElement>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [problem, setProblem] = useState<Trouble | null>(null);
  const [closed, setClosed] = useState(false);
  const [draft, setDraft] = useState("");
  const dead = problem?.restart === true;

  function listen(s: Session) {
    stream.current?.abort();
    const controller = new AbortController();
    stream.current = controller;
    const onEvent = (name: string, data: unknown) => {
      if (name !== "turn.status" || !inFlight.current) return;
      const label = typeof data === "object" && data !== null && "status" in data ? STATUS[String(data.status)] : undefined;
      setStatus(label ?? "");
    };
    // ponytail: best effort with no reconnect; the turn's own response is what counts.
    readEvents(s, onEvent, controller.signal).catch(() => undefined);
  }

  async function post(s: Session, request: TurnRequest, key: string): Promise<TurnResponse> {
    try {
      return await sendTurn(s, request, key);
    } catch (error) {
      if (!(error instanceof ApiError && error.code === "SESSION_BUSY")) throw error;
      await new Promise((resolve) => setTimeout(resolve, busyRetryMs));
      return sendTurn(s, request, key); // another turn held the session: once more, same key
    }
  }

  async function send(request: TurnRequest, key: string = crypto.randomUUID(), echo?: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setProblem(null);
    if (echo !== undefined) setEntries((e) => [...e, { from: "you", text: echo }]);
    pending.current = { request, key };
    try {
      let s = session.current;
      if (!s) {
        try {
          s = await createSession(locale);
        } catch {
          setProblem({ message: "We couldn't start a conversation. Please press Retry.", retry: true });
          return;
        }
        session.current = s;
        listen(s);
      }
      const turn = await post(s, request, key);
      pending.current = null;
      setEntries((e) => [...e, { from: "server", turn }]);
      setClosed(CLOSED_STATES.has(turn.state));
    } catch (error) {
      setProblem(trouble(error));
    } finally {
      inFlight.current = false;
      setBusy(false);
      setStatus("");
    }
  }

  function retry() {
    const p = pending.current;
    if (p) void send(p.request, p.key);
  }

  function restart() {
    if (inFlight.current) return;
    stream.current?.abort();
    session.current = null;
    pending.current = null;
    setEntries([]);
    setClosed(false);
    setProblem(null);
    setDraft("");
    void send(START);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || inFlight.current) return;
    setDraft("");
    void send({ text }, undefined, text);
  }

  const open = useEffectEvent(() => void send(START));
  const stop = useEffectEvent(() => stream.current?.abort());
  useEffect(() => {
    // Opening the chat starts a conversation with the API (an external system); the busy state it
    // sets costs one extra render on mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    open();
    return () => stop();
  }, []);

  // "start": a long reply shows from its top; a short one scrolls the page to its end, where the
  // sticky dock no longer covers the newest bubble ("nearest" left it hidden behind the dock). A
  // notice grows the dock, so it scrolls again.
  useEffect(() => {
    newest.current?.scrollIntoView?.({ block: "start" });
  }, [entries, problem, closed]);

  useEffect(() => {
    if (!busy) (closed || dead ? restartButton : input).current?.focus();
  }, [busy, closed, dead]);

  const last = entries.length - 1;
  return (
    <main>
      <h1>SurakshaSetu</h1>
      <ol className="log" role="log" aria-live="polite" aria-label="Conversation">
        {entries.map((entry, i) => (
          <li key={i} ref={i === last ? newest : undefined} className={entry.from}>
            <span className="sr-only">{entry.from === "you" ? "You:" : "SurakshaSetu:"}</span>
            {entry.from === "you" ? (
              <p>{entry.text}</p>
            ) : (
              <Reply
                turn={entry.turn}
                active={i === last && !busy && !dead}
                onPick={(reply) => void send({ action: reply.action }, undefined, reply.label)}
              />
            )}
          </li>
        ))}
      </ol>
      {/* The dock stays on screen: the turn's status, any notice and its buttons, the input. */}
      <div className="dock">
        <p className="status" role="status">
          {status}
        </p>
        {problem && (
          <div className="notice" role="alert">
            <p>{problem.message}</p>
            {problem.retry && (
              <button type="button" onClick={retry} disabled={busy}>
                Retry
              </button>
            )}
            {problem.restart && (
              <button type="button" ref={restartButton} onClick={restart}>
                New conversation
              </button>
            )}
          </div>
        )}
        {closed && !problem && (
          <div className="notice">
            <p>This conversation has ended.</p>
            <button type="button" ref={restartButton} onClick={restart} disabled={busy}>
              New conversation
            </button>
          </div>
        )}
        <form onSubmit={submit}>
          <label htmlFor="message" className="sr-only">
            Your message
          </label>
          <input
            id="message"
            ref={input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={closed || dead}
            autoComplete="off"
          />
          <button type="submit" disabled={busy || closed || dead || !draft.trim()}>
            Send
          </button>
        </form>
      </div>
    </main>
  );
}

/** A released message: its parts in order (plain text for now), then its quick replies. */
function Reply({ turn, active, onPick }: { turn: TurnResponse; active: boolean; onPick: (reply: QuickReply) => void }) {
  const { parts, text, quick_replies } = turn.message;
  // A blocked release has no parts: its text is the whole message.
  const paragraphs = parts.length ? parts.map((part) => part.text) : [text];
  return (
    <>
      {paragraphs.map((paragraph, i) => (
        <p key={i}>{paragraph}</p>
      ))}
      {quick_replies.length > 0 && (
        <div className="replies" role="group" aria-label="Choices">
          {quick_replies.map((reply, i) => (
            <button key={i} type="button" disabled={!active} onClick={() => onPick(reply)}>
              {reply.label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
