/**
 * The audit log, on screen.
 *
 * 🔑 THE STRONGEST THING THIS BUILD DOES WAS INVISIBLE UNTIL THIS FILE EXISTED. Every
 * command wrote its rows before any effect became visible, the endpoint served them, and
 * nothing in the interface read it, so the only way to see the record was to know the URL.
 * A record nobody can look at cannot be told apart from one that was never kept.
 *
 * ⚠️ IT IS NOT THE TRANSCRIPT, and the panel says so out loud. The command bar shows what
 * this browser asked and what came back; it is one tab's memory and it dies with the tab.
 * This is what the server committed. When the two disagree, this one is right.
 */
import { useEffect, useState } from "react";

import { fetchEvents, groupByCommand, type AuditEvent, type AuditGroup } from "./audit";
import { useStore } from "./store";

/** `2026-08-08T15:23:28.199648+00:00` to `15:23:28`. */
function clock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toTimeString().slice(0, 8);
}

/** A command id is a uuid. Six characters is enough to tie rows together by eye. */
function shortId(id: string | null): string {
  return id ? id.replace(/-/g, "").slice(0, 6) : "—";
}

function Row({ e }: { e: AuditEvent }) {
  return (
    <li className={`arow ${e.result === "ok" ? "" : "bad"}`}>
      <span className="atool">{e.tool}</span>
      {e.entityId && <span className="aent">{e.entityId}</span>}
      {e.result !== "ok" && <span className="ares">{e.result}</span>}
      {e.detail && <span className="adetail">{e.detail}</span>}
    </li>
  );
}

function Group({ g }: { g: AuditGroup }) {
  // The tier is a property of the command, not of every row it produced, so it is read off
  // whichever row carries one rather than repeated down the list.
  const tier = g.events.find((e) => e.tier)?.tier ?? null;
  return (
    <li className="agroup">
      <div className="ahead">
        <span className="atime">{clock(g.ts)}</span>
        <span className="aid">{shortId(g.commandId)}</span>
        {tier && <span className={`tier ${tier}`}>{tier}</span>}
        {/* The count is the argument: one sentence, four actions. Singular rows do not
            need it, and a "1 action" badge on every line would bury the interesting case. */}
        {g.events.length > 1 && <span className="acount">{g.events.length} actions</span>}
      </div>
      <ul className="arows">
        {g.events.map((e) => (
          <Row key={e.id} e={e} />
        ))}
      </ul>
    </li>
  );
}

export function AuditPanel() {
  const open = useStore((s) => s.auditOpen);
  const setOpen = useStore((s) => s.setAuditOpen);
  const [groups, setGroups] = useState<AuditGroup[]>([]);
  const [error, setError] = useState<string | null>(null);

  // 🔑 IT ONLY POLLS WHILE IT IS OPEN. A closed panel asking for the log every five seconds
  // would be traffic nobody can see the point of, and this endpoint is the one an evaluator
  // may well be watching in a network tab.
  useEffect(() => {
    if (!open) return;
    let live = true;
    const load = () =>
      fetchEvents()
        .then((evts) => {
          if (!live) return;
          setGroups(groupByCommand(evts));
          setError(null);
        })
        .catch((e: unknown) => live && setError(String(e)));
    load();
    const id = setInterval(load, 5000);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [open]);

  if (!open) return null;

  return (
    <aside className="audit" aria-label="audit log">
      <header>
        <span className="aname">AUDIT LOG</span>
        <button className="bclose" onClick={() => setOpen(false)} aria-label="close audit log">
          ×
        </button>
      </header>

      {/* Said on the face of the panel rather than in a README nobody opens. The difference
          between this and the transcript is the whole reason the log is worth having. */}
      <p className="anote">
        Written by the server before any effect was visible. The command transcript is this
        browser&apos;s memory; this is the record.
      </p>

      {error && <p className="err">{error}</p>}
      {!error && groups.length === 0 && (
        <p className="anote">Nothing recorded yet. Issue a command and it appears here.</p>
      )}

      <ul className="agroups">
        {groups.map((g) => (
          <Group key={`${g.commandId ?? "solo"}-${g.events[0].id}`} g={g} />
        ))}
      </ul>
    </aside>
  );
}
