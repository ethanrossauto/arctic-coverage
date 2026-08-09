/**
 * Turning audit rows into sentences a person can follow.
 *
 * 🔑 THE SERVER ALREADY WROTE MOST OF THE STORY. Every row's `detail` is prose composed at
 * the moment of the decision: "searched 17 available tools, selected list_entities",
 * "accepted 1 step(s): describe_entity", the exact refusal a tool produced. The old panel
 * printed that sentence and then dumped the parameter bag beside it as JSON, which made
 * the readable half look like decoration on the unreadable half. This module keeps the
 * sentence as the spine and converts every remaining fact into a labelled line, so
 * nothing needs a JSON reader and nothing is dropped.
 *
 * ⚠️ TRANSLATED, NEVER SUMMARISED. A summary picks what matters, which is exactly the
 * authority a log viewer must not have: the panel is the artifact somebody inspects to
 * decide whether the record is real. Every key in `params` is routed somewhere visible.
 * The routing table is explicit, and a key it does not know falls through to a generic
 * labelled pair rather than to silence, so a field added on the server shows up here by
 * default.
 */
import type { AuditEvent } from "./audit";

/** One labelled value: the legible form of one JSON key. */
export interface Fact {
  label: string;
  value: string;
}

/** One audit row, told as a step in the command's story. */
export interface Step {
  /** Who or what acted, in the vocabulary the code itself uses: parser, model, plan. */
  label: string;
  /** The result word, when it needs saying. `null` for ok: a step that reads as a plain
   *  sentence succeeded, and stamping "ok" on every line would bury the three words that
   *  matter, rejected, clarify and error, under fifty that do not. */
  outcome: string | null;
  tone: "ok" | "warn" | "bad";
  /** The narrative line. The server's own `detail` wherever one exists. */
  sentence: string;
  facts: Fact[];
  /** A plan or a model selection, one readable line per step, in order. */
  planLines: string[];
  /** Model, tokens, cache, cost: the cost of the call, one quiet line. */
  accounting: string | null;
}

/** How a command began: the words, and whether they were typed, spoken or a button. */
export interface Opening {
  how: string | null;
  text: string | null;
  /** The transcribe row the quote came from, so the panel can avoid printing the same
   *  words twice: once as the opening and once as that row's detail. */
  rowId: number | null;
}

/** The command sources, said the way the interface says them. A microphone row is
 *  "spoken" because that is the fact the operator cares about; "voice" is the wire word. */
export function sourceWord(source: string): string {
  if (source === "voice") return "spoken";
  if (source === "ui_button") return "button";
  return source;
}

/**
 * What the operator said to start this chain, and through which channel.
 *
 * 🔑 THE STORY OPENS WITH THE WORDS, which is the one ordering rule of the whole panel. A
 * spoken command's words live in the transcribe row's `detail`, written by the server the
 * moment the audio became text, so that row is searched for first: it is the origin even
 * when later rows in the chain say "typed". A typed command's words ride in
 * `params.utterance` on whichever row logged them.
 *
 * ⚠️ `null` text is a real answer, not a failure. A button posts a ready-made plan and no
 * sentence, the reset has no operator behind it at all, and a failed recording heard
 * nothing. Those chains open with their first step instead of with a quote.
 */
export function opening(events: AuditEvent[]): Opening {
  const heard = events.find((e) => e.tool === "transcribe" && e.result === "ok" && e.detail);
  if (heard) return { how: "spoken", text: heard.detail, rowId: heard.id };

  const carrier = events.find(
    (e) => typeof e.params?.utterance === "string" && e.params.utterance !== "",
  );
  if (carrier) {
    return {
      how: sourceWord(carrier.source),
      text: String(carrier.params?.utterance),
      rowId: null,
    };
  }

  const first = events[0];
  if (!first) return { how: null, text: null, rowId: null };
  if (first.source === "system" || first.actor === "system") {
    return { how: "system", text: null, rowId: null };
  }
  return { how: sourceWord(first.source), text: null, rowId: null };
}

/**
 * What each logging tool is, said as a role rather than an identifier.
 *
 * The vocabulary is the code's own: `parser.py` calls itself the parser, `llm.py` calls
 * itself the model, `index.py` comments the rate limiter as the spend guard. A real tool
 * (list_entities, place_asset) keeps its registry name, because those names are already
 * how the system talks about itself everywhere else on screen.
 *
 * ⚠️ `unsupported` maps to "parser" and `unparsed` to "model" on purpose. The first is
 * the parser recognising a request in order to decline it; the second is the model being
 * needed and not available. The raw tool word survives in the row's hover title, so the
 * database column stays checkable against what is displayed.
 */
const LABELS: Record<string, string> = {
  transcribe: "voice",
  tier1_parse: "parser",
  tier2_reason: "model",
  plan: "plan",
  rate_limited: "spend guard",
  unsupported: "parser",
  unparsed: "model",
  world_reset: "world",
};

/** Result words to tones. An unknown result reads as "warn" rather than as ok, because a
 *  new outcome the panel has never seen deserves attention, not camouflage. */
const TONES: Record<string, Step["tone"]> = {
  ok: "ok",
  escalated: "warn",
  clarify: "warn",
  rejected: "bad",
  error: "bad",
};

/**
 * The keys that are facts about the CALL rather than about the decision. They are still
 * shown, as one quiet line at the foot of the step, because "the model is only called
 * when it earns its latency" is this build's central claim and the price is the evidence.
 * The old panel hid them entirely, which protected the claim by hiding its receipts.
 */
const ACCOUNTING_KEYS = new Set([
  "model",
  "effort",
  "replay",
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "cost_usd",
  "audio_bytes",
]);

/**
 * The executor's placeholders, said the way the command transcript already says them.
 * Kept byte-identical to `readable()` in CommandBar.tsx: two renderers using two phrases
 * for one token would read as two different facts.
 */
const PLACEHOLDERS: Record<string, string> = {
  __viewport__: "the current view",
  __result__: "the previous answer",
  __selected__: "the selected asset",
  __subject__: "the plan's own subject",
};

/** `4315` to `4.3 s`, `522` to `522 ms`. */
export function formatLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

/** `74.9` to `74.9°N`, `-95.05` to `95.1°W` when asked for a longitude. */
function degrees(value: number, positive: string, negative: string): string {
  return `${Math.abs(value).toFixed(1)}°${value >= 0 ? positive : negative}`;
}

/** A viewport box as a place, not four floats. `global` means a pole-centred camera that
 *  spans every longitude, and naming it beats printing a west that exceeds its east. */
function boxWords(box: Record<string, unknown>): string {
  if (box.global) return "the whole globe";
  const edge = (k: string) => (typeof box[k] === "number" ? (box[k] as number) : null);
  const south = edge("south");
  const north = edge("north");
  const west = edge("west");
  const east = edge("east");
  if (south === null || north === null || west === null || east === null) {
    return JSON.stringify(box);
  }
  return (
    `${degrees(south, "N", "S")} to ${degrees(north, "N", "S")}, ` +
    `${degrees(west, "E", "W")} to ${degrees(east, "E", "W")}`
  );
}

/**
 * Any parameter value, as words.
 *
 * ⚠️ THE JSON.stringify AT THE BOTTOM IS THE LAST RESORT, NOT THE METHOD. Everything the
 * server writes today lands in a branch above it: placeholders become the phrases the
 * transcript uses, a viewport becomes coordinates with hemispheres, lists read as lists.
 * The fallback exists so a shape added on the server is displayed ugly rather than
 * dropped, which is the fail-visible ordering every check in this project follows.
 */
export function friendly(value: unknown): string {
  if (typeof value === "string") return PLACEHOLDERS[value] ?? value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value === null || value === undefined) return "none";
  if (Array.isArray(value)) {
    return value.length ? value.map(friendly).join(", ") : "none";
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if ("south" in record && "north" in record) return boxWords(record);
    return JSON.stringify(value);
  }
  return String(value);
}

/**
 * A plan or a model selection as numbered lines: `1. list_entities · kind uas`.
 *
 * One line per step because order is the content: "focus it, then describe it" and
 * "describe it, then focus it" are different commands, and an object dump loses exactly
 * that. The shape is `{tool, params}` from both tiers, which is the interchangeability
 * the two-tier design promises, so one renderer covers both.
 */
function planLines(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw, index) => {
    const step = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const tool = typeof step.tool === "string" && step.tool ? step.tool : "(unnamed tool)";
    const params = (
      step.params && typeof step.params === "object" ? step.params : {}
    ) as Record<string, unknown>;
    const pairs = Object.entries(params).map(([k, v]) => `${k} ${friendly(v)}`);
    return `${index + 1}. ${tool}${pairs.length ? ` · ${pairs.join(" · ")}` : ""}`;
  });
}

/** The cost line: model, effort, tokens, cache, dollars, audio size. Only what the row
 *  actually carries, so a parser step that cost nothing says nothing. */
function accounting(params: Record<string, unknown>): string | null {
  const parts: string[] = [];
  if (typeof params.model === "string") parts.push(params.model);
  if (typeof params.effort === "string") parts.push(`effort ${params.effort}`);
  if (params.replay) parts.push("replay fixture");

  const tin = params.input_tokens;
  const tout = params.output_tokens;
  if (typeof tin === "number" || typeof tout === "number") {
    parts.push(
      `tokens ${typeof tin === "number" ? tin : "?"} in, ${typeof tout === "number" ? tout : "?"} out`,
    );
  }

  if ("cache_read_tokens" in params || "cache_write_tokens" in params) {
    const read = typeof params.cache_read_tokens === "number" ? params.cache_read_tokens : 0;
    const wrote = typeof params.cache_write_tokens === "number" ? params.cache_write_tokens : 0;
    // "no cache use" rather than two zeroes: a cold cache is a fact worth a phrase, and
    // it is the exact symptom that once exposed a prompt too short to cache at all.
    parts.push(read === 0 && wrote === 0 ? "no cache use" : `cache ${read} read, ${wrote} written`);
  }

  if ("cost_usd" in params) {
    const cost = params.cost_usd;
    // An unpriced model logs null, deliberately, and $0.0000 here would claim a free
    // call that never happened. Same rule as the server's: unknown is not zero.
    parts.push(typeof cost === "number" ? `$${cost.toFixed(4)}` : "cost unpriced");
  }

  if (typeof params.audio_bytes === "number") {
    parts.push(`${(params.audio_bytes / 1024).toFixed(0)} kB of audio`);
  }
  return parts.length ? parts.join(" · ") : null;
}

/**
 * One row as one step of the story.
 *
 * 🔑 EVERY PARAMS KEY IS ROUTED, AND THE ROUTES ARE THE POINT. A key is allowed to leave
 * the facts line only when the same fact is already on screen in this group in a better
 * form: the utterance is the opening quote, `matched` is named verbatim inside the
 * parser's own sentence, `latency_ms` duplicates the row column the meta line shows.
 * Everything else is either given a shaped rendering (plans, candidates, the ignored
 * words) or falls through to a generic labelled pair. Omission needs a visible twin;
 * there is no other ground for it.
 */
export function describeEvent(e: AuditEvent, said: Opening): Step {
  const params: Record<string, unknown> = e.params ?? {};
  const facts: Fact[] = [];
  const lines: string[] = [];

  const utterance = typeof params.utterance === "string" ? params.utterance : null;
  if (utterance && utterance !== said.text) {
    facts.push({ label: "utterance", value: `"${utterance}"` });
  }

  for (const [key, value] of Object.entries(params)) {
    if (key === "utterance" || key === "matched" || key === "latency_ms") continue;
    if (ACCOUNTING_KEYS.has(key)) continue;
    if (key === "plan" || key === "selection") {
      lines.push(...planLines(value));
      continue;
    }
    if (key === "extracted") {
      // What the parser read OUT of the sentence, which is the half nobody can see from
      // the answer. Flattened to plain pairs: `kind uas` beats `extracted: {...}`.
      if (value && typeof value === "object" && !Array.isArray(value)) {
        for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
          facts.push({ label: k, value: friendly(v) });
        }
      }
      continue;
    }
    if (key === "ignored") {
      // The words the parser threw away, quoted one by one: the single most valuable
      // fact in the log, because a dropped word is invisible in an answer. An empty
      // list stays silent; the sentence "selected X" already means every word landed.
      if (Array.isArray(value) && value.length) {
        facts.push({
          label: "ignored",
          value: value.map((w) => `"${String(w)}"`).join(", "),
        });
      }
      continue;
    }
    if (key === "consumed") {
      // The complement of `ignored`, logged for symmetry. Words that DID land are
      // visible as the answer itself, so this collapses to a count rather than a list.
      if (Array.isArray(value) && value.length) {
        facts.push({ label: "used words", value: String(value.length) });
      }
      continue;
    }
    if (key === "clarify_candidates") {
      facts.push({ label: "offered", value: friendly(value) });
      continue;
    }
    if (key === "escalated_from") {
      facts.push({ label: "escalated from", value: friendly(value) });
      continue;
    }
    if (key === "used" || key === "limit") continue; // merged into one pair below
    facts.push({ label: key.replace(/_/g, " "), value: friendly(value) });
  }

  // The spend guard's two numbers as the one fact they express together.
  if ("used" in params || "limit" in params) {
    facts.push({ label: "used", value: `${friendly(params.used)} of ${friendly(params.limit)}` });
  }

  // The sentence is the server's own wherever one exists. The one substitution: a spoken
  // command's transcription is already the opening quote, and printing identical words
  // twice would make the reader hunt for a difference that is not there.
  let sentence = e.detail ?? "";
  if (said.rowId !== null && e.id === said.rowId) {
    sentence = "the recording became the words quoted above";
  }
  if (!sentence) sentence = `${e.result}, and the row carries no detail`;

  return {
    label: LABELS[e.tool] ?? e.tool,
    outcome: e.result === "ok" ? null : e.result,
    tone: TONES[e.result] ?? "warn",
    sentence,
    facts,
    planLines: lines,
    accounting: accounting(params),
  };
}
