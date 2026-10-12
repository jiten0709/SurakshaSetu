import { useEffect, useState } from "react";

// The dev inspector (Step 30): shown only by the dev server with VITE_INSPECTOR=1, never in a
// production build. Per turn, the FSM state, the transition row and its reason code, read from the
// audit trail's headers (GET /internal/audit/sessions/{id}/events: non-personal columns, payloads
// never sent). The dev server's proxy adds the compliance key; the browser never holds it. No CSS
// in index.css, so nothing of it reaches the shipped stylesheet either.

interface Row {
  from: string;
  to: string;
  row: string;
  reason: string | null;
}

type View = { rows: Row[] } | { note: string };

interface AuditEvent {
  event_type: string;
  header: Record<string, unknown>;
}

/** One row per STATE_TRANSITION (the decide node appends exactly one per turn); headers only. */
function transitions(events: AuditEvent[]): Row[] {
  return events
    .filter((e) => e.event_type === "STATE_TRANSITION")
    .map(({ header: h }) => ({
      from: String(h.from_state),
      to: String(h.to_state),
      row: String(h.trigger),
      reason: h.reason_code == null ? null : String(h.reason_code),
    }));
}

const line = (r: Row) => [r.row, r.reason].filter(Boolean).join(" · ");

export function Inspector({ sessionId, turns }: { sessionId: string | null; turns: number }) {
  const [view, setView] = useState<View>({ rows: [] });

  useEffect(() => {
    if (!sessionId) return;
    let stale = false;
    fetch(`/internal/audit/sessions/${sessionId}/events`)
      .then(async (r): Promise<View> => {
        if (r.status === 401 || r.status === 403) return { note: "not configured" }; // no or wrong key
        return r.ok ? { rows: transitions((await r.json()) as AuditEvent[]) } : { note: "unavailable" };
      })
      .catch((): View => ({ note: "unavailable" }))
      .then((v) => {
        if (!stale) setView(v);
      });
    return () => {
      stale = true;
    };
  }, [sessionId, turns]);

  const rows = "rows" in view ? view.rows : [];
  const latest = rows.at(-1);
  return (
    <aside aria-label="Inspector" style={{ fontSize: "0.8rem", color: "var(--muted)", paddingTop: 8 }}>
      <p style={{ margin: 0 }}>
        {"note" in view ? `Inspector: ${view.note}` : latest ? `${latest.to} · ${line(latest)}` : "Inspector: no turn yet"}
      </p>
      {rows.length > 0 && (
        <details>
          <summary>All turns ({rows.length})</summary>
          <ol style={{ maxHeight: "40vh", overflowY: "auto", margin: 0 }}>
            {rows.map((r, i) => (
              <li key={i}>{`${i + 1} · ${r.from} → ${r.to} · ${line(r)}`}</li>
            ))}
          </ol>
        </details>
      )}
    </aside>
  );
}
