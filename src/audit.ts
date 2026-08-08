/**
 * The audit log, read back out of the database.
 *
 * 🔑 THIS IS THE RECORD. The transcript in the command bar is not, and the distinction is
 * load-bearing rather than pedantic: the transcript is one browser's memory of what it
 * asked and is thrown away with the tab, while this is what the server wrote before any
 * effect became visible. If the two ever disagree, this one is right.
 *
 * ⚠️ IT WAS INVISIBLE UNTIL NOW, WHICH MADE IT UNCHECKABLE. Every row existed and the
 * endpoint served them, and nothing in the interface read it, so the only way to see the
 * strongest thing this build does was to know the URL. A record nobody can look at is
 * indistinguishable from one that was never kept.
 */

export interface AuditEvent {
  id: number;
  ts: string;
  commandId: string | null;
  parentCommandId: string | null;
  actor: string;
  source: string;
  tier: string | null;
  tool: string;
  result: string;
  detail: string | null;
  entityId: string | null;
  latencyMs: number | null;
  params: Record<string, unknown> | null;
}

interface WireEvent {
  id: number;
  ts: string;
  command_id: string | null;
  parent_command_id: string | null;
  actor: string;
  source: string;
  tier: string | null;
  tool: string;
  result: string;
  detail: string | null;
  entity_id: string | null;
  latency_ms: number | null;
  params: Record<string, unknown> | null;
}

/**
 * One command and everything it caused, newest command first.
 *
 * 🔑 GROUPED, BECAUSE THE GROUPING IS THE ARGUMENT. A flat list of rows shows that things
 * were logged. Rows gathered under the command that produced them show that one sentence
 * drove four actions, which is the claim worth being able to check.
 */
export interface AuditGroup {
  commandId: string | null;
  ts: string;
  events: AuditEvent[];
}

/** How many rows to hold. A panel to read, not a scrollback to search. */
export const AUDIT_LIMIT = 50;

export async function fetchEvents(): Promise<AuditEvent[]> {
  const res = await fetch(`/api/events?limit=${AUDIT_LIMIT * 4}`);
  if (!res.ok) throw new Error(`events request failed: ${res.status}`);
  const body = (await res.json()) as { events: WireEvent[] };
  return body.events.map((e) => ({
    id: e.id,
    ts: e.ts,
    commandId: e.command_id,
    parentCommandId: e.parent_command_id,
    actor: e.actor,
    source: e.source,
    tier: e.tier,
    tool: e.tool,
    result: e.result,
    detail: e.detail,
    entityId: e.entity_id,
    latencyMs: e.latency_ms,
    params: e.params,
  }));
}

/**
 * Gather rows under the command that caused them, newest command first.
 *
 * ⚠️ `parent_command_id` IS WHAT TIES A CHAIN TOGETHER, not `command_id` alone. A
 * clarification and the answer that follows it are separate commands linked by parent, and
 * an escalation to the second tier is a third row on the same thread. Grouping by
 * `command_id` alone would split one conversation into unrelated fragments, which is
 * exactly the story the log exists to keep whole.
 *
 * A row with no command id at all is its own group. The reset writes one of those, and it
 * is the first thing a viewer sees after the world goes back to seed.
 */
export function groupByCommand(events: AuditEvent[]): AuditGroup[] {
  const groups = new Map<string, AuditGroup>();
  for (const e of events) {
    const key = e.parentCommandId ?? e.commandId ?? `solo:${e.id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.events.push(e);
      // The group is stamped with its EARLIEST row: a command happened when it was asked,
      // not when its last side effect finished writing.
      if (e.ts < existing.ts) existing.ts = e.ts;
    } else {
      groups.set(key, { commandId: e.commandId ?? null, ts: e.ts, events: [e] });
    }
  }
  return [...groups.values()]
    .sort((a, b) => (a.ts < b.ts ? 1 : -1))
    .slice(0, AUDIT_LIMIT);
}
