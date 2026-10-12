import { useEffect, useEffectEvent, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  CLOSED_STATES,
  createSession,
  deleteSession,
  readEvents,
  sendTurn,
  type Action,
  type ConsentForm as Form,
  type Locale,
  type Session,
  type TurnRequest,
  type TurnResponse,
} from "./client";

// The chat screen: a thin client. Everything it shows comes from a released turn or a quick reply
// it was given, and everything it sends is typed text, a quick reply's or the consent form's
// action, a language switch or the erase.

type Entry = { from: "you"; text: string } | { from: "server"; turn: TurnResponse };
type Part = TurnResponse["message"]["parts"][number];

/** A reply that presents the S3 options (a recommendation render). */
const hasCards = (entry: Entry) =>
  entry.from === "server" && entry.turn.message.parts.some((p) => p.id.startsWith("option_card:"));

interface Trouble {
  message: string;
  retry?: boolean;
  restart?: boolean;
  gone?: boolean; // the session is already erased: a plain result, not an error
}

const START: TurnRequest = { action: { type: "START", payload: {} } };
const ERASE: TurnRequest = { action: { type: "ERASE", payload: {} } };
const ERASE_LABEL = "Erase my data";

/** Each locale's name in its own language: the switch offers the other one. */
const LANGUAGES: Record<Locale, string> = { "en-IN": "English", "hi-IN": "हिंदी" };
const other = (locale: Locale): Locale => (locale === "en-IN" ? "hi-IN" : "en-IN");

const erasing = (request: TurnRequest) => "action" in request && request.action.type === "ERASE";

/** The locale a language action asks for, if the request is one. */
function asked(request: TurnRequest): Locale | undefined {
  if (!("action" in request)) return undefined;
  const { action } = request;
  if (action.type === "LANGUAGE") return action.payload.locale;
  if (action.type === "NOTICE_LANGUAGE") return action.payload.language;
  return undefined;
}

const STATUS: Record<string, string> = {
  analysing: "Reading your message…",
  composing: "Writing a reply…",
  validating: "Checking the reply…",
};

const UNREACHABLE = "We couldn't reach the service. Nothing was lost: press Retry.";
const GENERIC = "Something went wrong.";

/** What to tell the customer, and what they can do, per failure (the API's problem codes). */
function trouble(error: unknown, erase: boolean): Trouble {
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
      // An erase on a session already gone is the spec's repeat (401): its plain result.
      if (erase) return { message: "This conversation has already been erased.", restart: true, gone: true };
      return { message: "This conversation is no longer available. Please start a new one.", restart: true };
    case "SERVICE_UNAVAILABLE":
      return { message: UNREACHABLE, retry: true };
    case "REQUEST_ENTITY_TOO_LARGE":
      return { message: "That message is too long. Please shorten it." };
    default:
      return { message: `${GENERIC} Please press Retry.`, retry: true };
  }
}

export function Chat({ locale: initial = "en-IN", busyRetryMs = 1000 }: { locale?: Locale; busyRetryMs?: number }) {
  const session = useRef<Session | null>(null); // the token lives here, in memory only
  const stream = useRef<AbortController | null>(null);
  const inFlight = useRef(false); // synchronous, so a double click sends one turn
  const eraseNext = useRef(false); // an erase confirmed while a turn ran: it goes next, nothing else
  const pending = useRef<{ request: TurnRequest; key: string } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const restartButton = useRef<HTMLButtonElement>(null);
  const eraseButton = useRef<HTMLButtonElement>(null);
  const newest = useRef<HTMLLIElement>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [problem, setProblem] = useState<Trouble | null>(null);
  const [closed, setClosed] = useState(false);
  const [draft, setDraft] = useState("");
  // The session's locale as the server answered it: the form's language, else the last switch it
  // took. The released turn names no locale, so a typed switch after S0 is not seen here.
  const [locale, setLocale] = useState<Locale>(initial);
  const [live, setLive] = useState(false); // a session is held (the render cannot read the ref)
  const [confirming, setConfirming] = useState(false);
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

  /** Drop the session: its token, its event stream and the controls that need one. */
  function forget() {
    stream.current?.abort();
    session.current = null;
    setLive(false);
  }

  async function post(s: Session, request: TurnRequest, key: string): Promise<TurnResponse> {
    // The erase is DELETE /v1/sessions/{id}: the same turn as the ERASE action.
    const deliver = () => (erasing(request) ? deleteSession(s, key) : sendTurn(s, request, key));
    try {
      return await deliver();
    } catch (error) {
      if (!(error instanceof ApiError && error.code === "SESSION_BUSY")) throw error;
      await new Promise((resolve) => setTimeout(resolve, busyRetryMs));
      return deliver(); // another turn held the session: once more, same key
    }
  }

  async function send(request: TurnRequest, key: string = crypto.randomUUID(), echo?: string) {
    const erase = erasing(request);
    // An erase never opens a session. A queued erase starts in `finally`, in the same tick that
    // frees `inFlight`, so no other turn can start before it.
    if (inFlight.current || (erase && !session.current)) return;
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
        setLive(true);
        listen(s);
      }
      const turn = await post(s, request, key);
      pending.current = null;
      setEntries((e) => [...e, { from: "server", turn }]);
      setClosed(CLOSED_STATES.has(turn.state));
      setLocale((current) => turn.message.form?.language ?? asked(request) ?? current);
      // The server has deleted the session: after any erase (a closed state keeps its own state
      // name, EXIT.STAY), and whenever a turn enters Data Erasure (a typed withdrawal, a minor).
      if (erase || turn.state === "DATA_ERASURE") forget();
    } catch (error) {
      const t = trouble(error, erase);
      setProblem(t);
      if (t.gone) forget();
    } finally {
      inFlight.current = false;
      setBusy(false);
      setStatus("");
      if (eraseNext.current) {
        eraseNext.current = false;
        void send(ERASE, undefined, ERASE_LABEL);
      }
    }
  }

  function retry() {
    const p = pending.current;
    if (p) void send(p.request, p.key);
  }

  function restart() {
    if (inFlight.current) return;
    forget();
    pending.current = null;
    setEntries([]);
    setClosed(false);
    setProblem(null);
    setDraft("");
    setConfirming(false);
    void send(START); // in the language last chosen
  }

  /** The confirmed erase; behind a turn in flight, it waits for that turn's reply. */
  function erase() {
    setConfirming(false);
    if (inFlight.current) eraseNext.current = true;
    else void send(ERASE, undefined, ERASE_LABEL);
  }

  function cancelErase() {
    setConfirming(false);
    eraseButton.current?.focus();
  }

  /** NOTICE_LANGUAGE while S0's consent form is open, LANGUAGE afterwards (the spec's actions). */
  function switchLanguage() {
    const to = other(locale);
    const latest = entries.findLast((e) => e.from === "server");
    const formOpen = latest?.from === "server" && latest.turn.message.form !== null;
    const action: Action = formOpen
      ? { type: "NOTICE_LANGUAGE", payload: { language: to } }
      : { type: "LANGUAGE", payload: { locale: to } };
    void send({ action }, undefined, LANGUAGES[to]);
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
    open();
    return () => stop();
  }, []);

  // "start": a long reply shows from its top; a short one scrolls the page to its end, where the
  // sticky dock no longer covers the newest bubble ("nearest" left it hidden behind the dock). A
  // notice or the erase confirmation grows the dock, so it scrolls again.
  useEffect(() => {
    newest.current?.scrollIntoView?.({ block: "start" });
  }, [entries, problem, closed, confirming]);

  useEffect(() => {
    if (!busy) (closed || dead ? restartButton : input).current?.focus();
  }, [busy, closed, dead]);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const last = entries.length - 1;
  // A recommendation rendered again (a language switch, an acknowledgment mismatch, a re-ranking)
  // leaves the older render in the log, readable but muted; its buttons are off like any old reply's.
  const lastRender = entries.findLastIndex(hasCards);
  const switchTo = other(locale);
  return (
    <main>
      <h1>SurakshaSetu</h1>
      <ol className="log" role="log" aria-live="polite" aria-label="Conversation">
        {entries.map((entry, i) => (
          <li
            key={i}
            ref={i === last ? newest : undefined}
            className={i < lastRender && hasCards(entry) ? `${entry.from} superseded` : entry.from}
          >
            <span className="sr-only">{entry.from === "you" ? "You:" : "SurakshaSetu:"}</span>
            {entry.from === "you" ? (
              <p>{entry.text}</p>
            ) : (
              <Reply
                turn={entry.turn}
                active={i === last && !busy && !dead}
                onAction={(action, label) => void send({ action }, undefined, label)}
              />
            )}
          </li>
        ))}
      </ol>
      {/* The dock stays on screen: the turn's status, any notice and its buttons, the input, and the
          language switch and erase (in a header they scrolled away with the first long reply). */}
      <div className="dock">
        <p className="status" role="status">
          {status}
        </p>
        {confirming && (
          <div className="notice">
            <p>Erase your data and end this conversation?</p>
            <button type="button" onClick={erase}>
              Erase
            </button>
            {/* Focus lands on the safe choice. */}
            <button type="button" onClick={cancelErase} autoFocus>
              Cancel
            </button>
          </div>
        )}
        {problem && (
          <div className="notice" role={problem.gone ? "status" : "alert"}>
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
        <div className="tools">
          <button
            type="button"
            className="secondary"
            lang={switchTo}
            onClick={switchLanguage}
            disabled={busy || !live || closed || dead}
          >
            {LANGUAGES[switchTo]}
          </button>
          {/* Usable in any state and while a turn runs (the erase then waits for it): I5. */}
          {live && (
            <button type="button" className="secondary" ref={eraseButton} onClick={() => setConfirming(true)}>
              {ERASE_LABEL}
            </button>
          )}
        </div>
      </div>
    </main>
  );
}

type OnAction = (action: Action, label: string) => void;

/** A released message: its parts in the order sent, its consent form, its quick replies. */
function Reply({ turn, active, onAction }: { turn: TurnResponse; active: boolean; onAction: OnAction }) {
  const { parts, text, form, quick_replies } = turn.message;
  return (
    <>
      {/* A blocked release has no parts: its text is the whole message. */}
      {parts.length ? parts.map((part, i) => <PartView key={i} part={part} />) : <p>{text}</p>}
      {form && <ConsentForm form={form} active={active} onSubmit={onAction} />}
      {quick_replies.length > 0 && (
        <div className="replies" role="group" aria-label="Choices">
          {quick_replies.map((reply, i) => (
            <button key={i} type="button" disabled={!active} onClick={() => onAction(reply.action, reply.label)}>
              {reply.label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

/**
 * One part, by its id (the spec's namespaces), its text whole and as sent: the S3 sections only gain
 * structure (a card, a table, a disclosure set, the sources panel), headed by their own first line.
 * Anything else, an unknown id included, is a plain paragraph. Nothing is reordered or reworded.
 */
function PartView({ part: { id, text } }: { part: Part }) {
  const [head, ...rest] = text.split("\n");
  const body = rest.length > 0 && <p>{rest.join("\n")}</p>;
  if (id.startsWith("option_card:")) {
    return (
      <article className="card" aria-label={head} data-part={id}>
        <h2>{head}</h2>
        {body}
      </article>
    );
  }
  if (id.startsWith("disclosures:")) {
    // Legal text: always whole and open, never collapsed, clamped or truncated.
    return (
      <section className="disclosures" aria-label={head} data-part={id}>
        <h2>{head}</h2>
        {body}
      </section>
    );
  }
  if (id === "sources" || id === "template:sources") {
    return (
      <aside className="sources" aria-label={head} data-part={id}>
        <h2>{head}</h2>
        <ol>
          {rest.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ol>
      </aside>
    );
  }
  return (id === "comparison" && comparison(id, text)) || <p data-part={id}>{text}</p>;
}

/** The composer's comparison (a caption line, then a pipe table) as a table; null if it isn't one. */
function comparison(id: string, text: string) {
  const [caption, header, rule, ...rows] = text.split("\n");
  const cells = (row = "") => (row.startsWith("|") && row.endsWith("|") ? row.slice(1, -1).split("|").map((c) => c.trim()) : null);
  // ponytail: a "|" inside a cell breaks the shape, and the part is then shown as plain text.
  const head = cells(header);
  const grid = rows.map(cells);
  if (!head || rule !== `${"|---".repeat(head.length)}|` || grid.some((r) => r?.length !== head.length)) return null;
  return (
    <div className="scroll" data-part={id}>
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            <td>{head[0]}</td>
            {head.slice(1).map((cell, i) => (
              <th key={i} scope="col">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(grid as string[][]).map(([label, ...values], i) => (
            <tr key={i}>
              <th scope="row">{label}</th>
              {values.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * S0's consent form as the server sent it: its labels verbatim, nothing ticked, submitted as
 * CONSENT_SUBMIT with the form's own notice fields. Live only on the latest reply, so the submission's
 * own echo turns it off for good. No hash and no validation here: the server answers (P1 unticked
 * is a refusal it handles).
 */
function ConsentForm({ form, active, onSubmit }: { form: Form; active: boolean; onSubmit: OnAction }) {
  const [ticked, setTicked] = useState<Record<string, boolean>>({});

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!active) return;
    const purposes = { P1: ticked.P1 === true, P2: ticked.P2 === true, P3: ticked.P3 === true };
    const payload = {
      purposes,
      age_18_plus: ticked.adult === true,
      notice_version: form.notice_version,
      notice_sha256: form.notice_sha256,
    };
    onSubmit({ type: "CONSENT_SUBMIT", payload }, form.submit);
  }

  const box = (id: string, label: string) => (
    <label key={id}>
      <input
        type="checkbox"
        checked={ticked[id] === true}
        disabled={!active}
        onChange={(e) => {
          const checked = e.target.checked;
          setTicked((t) => ({ ...t, [id]: checked }));
        }}
      />
      <span>{label}</span>
    </label>
  );

  return (
    <form className="consent" aria-label="Consent form" onSubmit={submit}>
      {form.purposes.map((p) => box(p.id, p.label))}
      {box("adult", form.adult.label)}
      <button type="submit" disabled={!active}>
        {form.submit}
      </button>
    </form>
  );
}
