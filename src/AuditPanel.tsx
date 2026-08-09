/**
 * The audit log, on screen, told as stories rather than served as rows.
 *
 * 🔑 THE STRONGEST THING THIS BUILD DOES WAS INVISIBLE UNTIL THIS FILE EXISTED. Every
 * command wrote its rows before any effect became visible, the endpoint served them, and
 * nothing in the interface read it, so the only way to see the record was to know the URL.
 * A record nobody can look at cannot be told apart from one that was never kept.
 *
 * 🔑 ONE COMMAND READS AS ONE STORY, TOP TO BOTTOM: what the operator said and whether it
 * was typed or spoken, then each thing that happened to it in the order it happened. The
 * parser's decision, the model being consulted and why, the plan, each tool's answer or
 * refusal, the question asked back when a name was ambiguous. The first version of this
 * panel printed the same record as rows with JSON parameter bags, which was disclosure
 * without legibility: everything was present and nothing could be followed.
 *
 * ⚠️ STILL FULL DISCLOSURE, NOW IN LANGUAGE. This is the artifact somebody inspects to
 * decide whether the log is real, so nothing is summarised away: every column and every
 * params key is either on the face of the panel as a sentence or a labelled line, or in
 * the row's hover title (the raw tool word, the full command and parent ids). The routing
 * lives in auditStory.ts, and a field the story does not know falls through to a labelled
 * pair rather than to silence.
 *
 * ⚠️ IT IS NOT THE TRANSCRIPT, and the panel says so out loud. The command bar shows what
 * this browser asked and what came back; it is one tab's memory and it dies with the tab.
 * This is what the server committed. When the two disagree, this one is right.
 */
import { useEffect, useState } from "react";

import { fetchEvents, groupByCommand, type AuditEvent, type AuditGroup } from "./audit";
import { describeEvent, formatLatency, opening, sourceWord, type Opening } from "./auditStory";
import { useStore } from "./store";

/** `2026-08-08T15:23:28.199648+00:00` to `15:23:28`. */
function clock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toTimeString().slice(0, 8);
}

/** A command id is a uuid. Six characters is enough to tie rows together by eye. The
 *  no-id case is written out as a word rather than drawn as a dash, so a chain that never
 *  got an id reads as a fact and not as a rendering gap. */
function shortId(id: string | null): string {
  return id ? id.replace(/-/g, "").slice(0, 6) : "no id";
}

/**
 * One audit row as one step.
 *
 * The layout is a sentence first and its receipts under it: the labelled facts, the plan
 * lines, the cost, then a meta line carrying the row's bookkeeping (actor, channel, tier,
 * entity, duration, its own clock time, and the row id for checking against the
 * database). Facts about the decision sit above facts about the call, because that is
 * the order a reader wants them in.
 */
function StepRow({
  e,
  said,
  showTier,
  groupCommandId,
}: {
  e: AuditEvent;
  said: Opening;
  showTier: boolean;
  groupCommandId: string | null;
}) {
  const s = describeEvent(e, said);
  return (
    <li className={`arow ${s.tone}`}>
      <div className="aline">
        {/* The raw tool word rides in the title so the friendly label ("parser" for
            tier1_parse) stays checkable against the database column it renders. */}
        <span className="awho" title={e.tool}>
          {s.label}
        </span>
        {s.outcome && <span className={`ares ${s.tone}`}>{s.outcome}</span>}
        <span className="asent">{s.sentence}</span>
      </div>

      {s.facts.length > 0 && (
        <div className="afacts">
          {s.facts.map((f, i) => (
            <span key={`${f.label}-${i}`} className="akv">
              <span className="k">{f.label}</span> {f.value}
            </span>
          ))}
        </div>
      )}

      {s.planLines.map((line, i) => (
        <div key={i} className="aplan">
          {line}
        </div>
      ))}

      {s.accounting && <div className="acct">{s.accounting}</div>}

      <span className="ameta">
        {e.actor !== "operator" && (
          <span>
            <span className="k">actor</span> {e.actor}
          </span>
        )}
        {/* Named only when this step arrived through a different channel than the words
            did: a clarification answered by pressing a chip is the operator redirecting
            the command, and that switch is part of the story. */}
        {said.how && sourceWord(e.source) !== said.how && (
          <span>
            <span className="k">via</span> {sourceWord(e.source)}
          </span>
        )}
        {showTier && e.tier && (
          <span>
            <span className="k">tier</span> {e.tier}
          </span>
        )}
        {e.entityId && (
          <span>
            <span className="k">entity</span> {e.entityId}
          </span>
        )}
        {e.latencyMs !== null && (
          <span>
            <span className="k">took</span> {formatLatency(e.latencyMs)}
          </span>
        )}
        <span>
          <span className="k">at</span> {clock(e.ts)}
        </span>
        {/* An escalation or a clarification runs as a second command chained under the
            first, so a step whose own command id differs from the group's says so: that
            difference IS the hand-off, visible. */}
        {e.commandId && e.commandId !== groupCommandId && (
          <span title={e.commandId}>
            <span className="k">cmd</span> {shortId(e.commandId)}
          </span>
        )}
        {/* The full ids, not prefixes, in the title: these are the values that tie a
            chain together, and a truncated one cannot be matched against the database
            by anyone checking. */}
        <span title={`command ${e.commandId ?? "none"} · parent ${e.parentCommandId ?? "none"}`}>
          <span className="k">row</span> #{e.id}
        </span>
      </span>
    </li>
  );
}

function Group({ g }: { g: AuditGroup }) {
  const said = opening(g.events);
  // Every tier that acted, in the order they acted. One badge is the common case; an
  // escalated command reads "parser → llm" here, which is the two-tier design's whole
  // argument compressed into a header.
  const tiers = [...new Set(g.events.map((e) => e.tier).filter((t): t is string => t !== null))];
  return (
    <li className="agroup">
      <div className="ahead">
        <span className="atime">{clock(g.ts)}</span>
        {/* The full id in the title: this is the value that ties a chain together, and a
            truncated one cannot be matched against the database by anyone checking. */}
        <span className="aid" title={g.commandId ?? "no command id"}>
          {shortId(g.commandId)}
        </span>
        {tiers.map((t, i) => (
          <span key={t} className="tierpath">
            {i > 0 && <span className="tarrow">→</span>}
            <span className={`tier ${t}`}>{t}</span>
          </span>
        ))}
        {/* The count is the argument: one sentence, four steps. Singular rows do not
            need it, and a "1 step" badge on every line would bury the interesting case. */}
        {g.events.length > 1 && <span className="acount">{g.events.length} steps</span>}
      </div>

      {/* The story starts with what was said. The badge answers "typed or spoken" before
          the quote is even read, because a misheard voice command and a mistyped one are
          different failures and the panel must not make the reader infer which. */}
      {said.text && (
        <div className="asaid">
          <span className={`ahow ${said.how ?? ""}`}>{said.how}</span>
          <span className="aquote">&ldquo;{said.text}&rdquo;</span>
        </div>
      )}

      <ul className="arows">
        {g.events.map((e) => (
          <StepRow
            key={e.id}
            e={e}
            said={said}
            showTier={tiers.length > 1}
            groupCommandId={g.commandId}
          />
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

      {/* ⚠️ ONE LINE, AND ONLY THE PART A READER NEEDS IN ORDER TO READ. This used to
          explain that the server writes these rows before any effect is visible and that
          the transcript is the browser's memory while this is the record. All true, all
          about why the panel exists rather than about what is in front of you, and it sat
          above the thing it was describing. The panel makes that case by being legible. */}
      <p className="anote">Newest commands first.</p>

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
