/**
 * The command surface: a typing field, a microphone, and the activity log.
 *
 * 🔑 ONE ENDPOINT, ONE VALIDATOR, ONE LOG. Typing and speaking are both just an
 * utterance with a different `source`. Voice is an input method, not a second command
 * path, so nothing downstream of this file knows or cares which was used.
 *
 * 🔒 THE SUMMARY SHOWN HERE IS THE SERVER'S, written by the executor from what actually
 * ran. It is deliberately never the model's own words: a model-written summary is a
 * claim about the world, and the executor's is a report of it. Those two diverge exactly
 * when it matters most, which is when a plan half-failed.
 *
 * The tier is shown per entry on purpose. "The model is only called when it earns its
 * latency" is the central design claim, and putting the tier on screen makes it
 * checkable by anyone using the thing rather than a sentence in a README.
 */
import { useEffect, useRef, useState } from "react";

import { ALL_KINDS, fetchAssets, fetchMesh } from "./assets";
import { setCommandRunner } from "./commandRunner";
import { HelperSheet, helperMuted, setMuted } from "./HelperSheet";
import type { AssetKind } from "./assets";
import { RECENT_IDS, useStore } from "./store";
import type { AssetTrack, CameraTarget } from "./store";

/** One candidate the operator can pick when a command was ambiguous. */
interface ClarifyOption {
  id: string;
  label: string;
  detail: string;
  /**
   * A runnable plan, posted straight back. `null` is a real case and means DO NOT render a
   * button: the vague phrase could not be substituted, so a button there would re-ask the
   * same question forever.
   */
  plan: unknown[] | null;
}

interface Clarify {
  command_id: string;
  query: string;
  question: string;
  /** How many matched. `options` may be fewer, capped at six. */
  total: number;
  options: ClarifyOption[];
}

/**
 * What the server may ask the client to do after a plan runs.
 *
 * ⚠️ EFFECTS MERGE BY KEY ACROSS A MULTI-STEP PLAN, LAST WRITER WINS. One response can now
 * carry effects produced by several steps, so this is not "one effect per command". Nothing
 * here may assume it is the only one of its kind.
 */
interface UiEffects {
  camera?: CameraTarget;
  select?: string | null;
  /** Asset ids the answer points at. ⚠️ An EMPTY ARRAY MEANS CLEAR, not "no change". */
  highlight?: string[];
  /** The world changed; refetch rather than patching a second model of it locally. */
  refetch?: true;
  /** Turn a layer the client already draws on or off. No server code reads that layer's data. */
  overlay?: { layer: "ice"; visible: boolean };
  /**
   * Which KINDS are drawn. An intent, not a computed set.
   *
   * 🔑 THE SERVER CANNOT SEND A SET BECAUSE IT DOES NOT KNOW ONE. Which kinds are
   * switched off is this browser's preference and lives here, so the server names what
   * it was asked to do and this side resolves it. `only` and `all` are absolute, which
   * is what makes "show only the vessels" mean the same thing wherever you started.
   */
  kinds?: { mode: "hide" | "show" | "only" | "all"; kinds: AssetKind[] };
  /** The command was ambiguous. Offer the candidates rather than guessing. */
  clarify?: Clarify;
  /**
   * A position series for one asset, oldest first, lon-first.
   *
   * ⚠️ It arrives with a `camera` already framed to it, so nothing here adds a second
   * camera move on top.
   */
  track?: AssetTrack;
}

interface CommandResponse {
  ok: boolean;
  command_id?: string;
  summary: string;
  tier: string | null;
  /** Present only when the parser tried, failed to resolve, and the model was asked. */
  escalated_from?: string;
  /**
   * After a model answer only: the deterministic phrasing that reaches the same tool.
   *
   * ⚠️ NOT A TRANSLATION OF THE QUESTION. The tool is what matches, not the sentence, so
   * the line is worded as "reachable without the model" rather than "next time say this".
   */
  teach?: { tool: string; say: string }[];
  /**
   * Why the answer is what it is. Present on both tiers, with different keys on each.
   *
   * ⚠️ THE KEY IS `thinking`, AND I HAD THIS WRONG. This renderer originally read
   * `reasoning` and `trace` at the top level, found neither, and reported that the server
   * was not sending any. It had been sending this for hours. A field that is simply absent
   * looks identical to a field that was never built.
   */
  thinking?: {
    tier?: string;
    /** Tier 2: the model's account of what it decided. */
    reasoning?: string;
    model?: string;
    latency_ms?: number;
    cost_usd?: number;
    /** Tier 1: which tool answered, and what it took out of the sentence. */
    matched?: string;
    extracted?: Record<string, unknown>;
    /**
     * The declared sentence that answered, which is exactly what the reference card prints.
     *
     * 🔑 THE ONE FIELD AN OPERATOR CAN ACT ON. "matched list_entities" names a function nobody
     * says out loud; the phrasing that answered them is repeatable, and seeing it here is the
     * card arriving at the moment they are already looking at the screen.
     */
    grammar?: string;
    /**
     * Why tier 1 handed over, present only when it did.
     *
     * 🔑 ITS EXISTENCE IS THE MESSAGE, and it replaced `ignored` below. Tier 1 is an exact
     * grammar now: it cannot half-match, so there are no dropped words to name. What it can
     * say is whether the sentence was NEAR a declared command, which is the difference between
     * "say it the printed way and it is instant and free" and "this genuinely needed the
     * model".
     */
    declined?: string;
    /**
     * Words the parser threw away.
     *
     * ⚠️ NO LONGER SENT, AND KEPT HERE FOR THE ROWS THAT ALREADY CARRY IT. Tier 1 was a pile
     * of patterns that could match part of a sentence, so two utterances differing only by "on
     * foot" returned identical answers with the phrase silently dropped, and this list was how
     * that became visible. An anchored grammar accounts for every word or produces no plan, so
     * nothing computes this any more. The audit log still holds it on older commands, and what
     * those rows say about what happened then is still true.
     */
    ignored?: string[];
    /**
     * The parser's own finding, present ONLY on an escalated response.
     *
     * 🔑 ITS EXISTENCE IS THE MESSAGE: tier 1 looked, came up short, and handed over. A
     * phrasing the parser never recognised at all has no trace here, because "ignored:
     * nothing" would imply an opinion it does not have.
     *
     * ⚠️ It has to be read separately rather than merged, because the escalated response's
     * top-level fields are the MODEL's. Reading `extracted` off the top level on an
     * escalated answer finds null: the parser's work is nested here precisely so the
     * handover does not erase it.
     */
    parser?: {
      matched?: string;
      extracted?: Record<string, unknown>;
      grammar?: string;
      declined?: string;
      ignored?: string[];
    };
  };
  results: {
    tool: string;
    ok: boolean;
    message: string;
    /** Entities the step returned. `data.ids` is what "them" binds to next turn. */
    data?: { ids?: string[] };
  }[];
  ui_effects?: UiEffects;
}

/**
 * Turn whatever the response says about its own reasoning into one readable block.
 *
 * ⚠️ TIER 1's TRACE IS THE MORE USEFUL OF THE TWO, and `ignored` is why. Two utterances
 * differing only by "on foot" returned byte-identical answers because the parser silently
 * dropped the phrase. Nothing on screen said so. Printing what was ignored turns that from
 * invisible into obvious.
 */
function thinkingOf(body: CommandResponse): string | undefined {
  const t = body.thinking;
  if (!t) return undefined;
  const lines: string[] = [];

  // 🔑 THE PARSER'S FINDING GOES FIRST ON AN ESCALATED ANSWER, because it is the reason the
  // model was called at all. Read in the other order the story is backwards: here is what
  // the model concluded, and separately, here is a tier that apparently did nothing.
  const p = t.parser;
  if (p?.ignored?.length) {
    // Only reachable for a command answered before the grammar landed; see `ignored` above.
    lines.push(
      `parser: dropped ${p.ignored.map((w) => `"${w}"`).join(", ")} → asked the model`,
    );
  } else if (p?.declined) {
    lines.push(`parser: ${p.declined} → asked the model`);
  } else if (p?.matched) {
    lines.push(`parser: matched ${p.matched} → asked the model`);
  }
  if (p?.extracted && Object.keys(p.extracted).length) {
    lines.push(`  parser read: ${paramList(p.extracted)}`);
  }

  if (t.reasoning) lines.push(`${p ? "model:  " : ""}${t.reasoning}`);
  if (t.grammar) lines.push(`matched: "${t.grammar}" → ${t.matched}, no model call`);
  else if (t.matched) lines.push(`matched: ${t.matched}`);
  if (t.extracted && Object.keys(t.extracted).length) {
    lines.push(`extracted: ${paramList(t.extracted)}`);
  }
  if (t.ignored?.length) lines.push(`ignored: ${t.ignored.join(", ")}`);

  // The claim this project makes is that the model runs only when it earns its latency.
  // Printing what the call actually cost makes that checkable by whoever is using the
  // thing, rather than a sentence in a README taken on trust.
  const meta = [
    t.model,
    t.latency_ms !== undefined ? `${(t.latency_ms / 1000).toFixed(1)}s` : null,
    t.cost_usd !== undefined ? `$${t.cost_usd.toFixed(4)}` : null,
  ].filter(Boolean);
  if (meta.length) lines.push(meta.join("  ·  "));

  return lines.length ? lines.join("\n") : undefined;
}

function paramList(o: Record<string, unknown>): string {
  return Object.entries(o)
    .map(([k, v]) => `${k}=${readable(v)}`)
    .join(", ");
}

/**
 * Render a parameter the way a person would read it.
 *
 * ⚠️ `__viewport__` and `__result__` are real answers, not noise. They are what the parser
 * genuinely decided the operator meant by "the current window" or "them"; the substitution
 * happens later and elsewhere. Showing the raw token would read as a bug, and hiding it
 * would lose the most interesting thing the parser did.
 */
function readable(v: unknown): string {
  if (v === "__viewport__") return "the current view";
  if (v === "__result__") return "the previous answer";
  if (v === "__selected__") return "the selected asset";
  return typeof v === "string" ? v : JSON.stringify(v);
}

/**
 * How long a command may run before the transcript says something, in milliseconds.
 *
 * Measured rather than chosen: tier 1 answers in 1.4 to 1.6 seconds warm and tier 2 in 12
 * to 22. Anything in this gap separates "answered instantly" from "still working" without
 * making the fast path flicker.
 */
const THINKING_AFTER_MS = 1200;

/**
 * What the history says when the request itself did not complete.
 *
 * 🔑 IT DESCRIBES THE SITUATION, NOT THE MECHANISM. The operator does not need to know
 * whether it was a status code, a dropped socket or a body that would not parse; they need
 * to know the console is not talking to the world and that trying again is reasonable.
 * Every diagnostic version of this sentence ends up quoting an error class at somebody who
 * cannot act on it.
 */
const UNREACHABLE =
  "the console could not reach the world just then. Try that again in a moment.";

/**
 * How many lines survive a collapse.
 *
 * 🔑 FIVE, WHICH IS MORE THAN ONE EXCHANGE ON PURPOSE. A command is two lines, its words
 * and the answer, and a spoken one that needed a correction is three. Cutting to exactly
 * one exchange kept losing the context that made the latest answer make sense, so this
 * holds the last one whole plus whatever came before it fits.
 */
const LATEST_EXCHANGE = 5;

/**
 * Did the correction actually change anything the operator would care about?
 *
 * 🔴 A NOTICE THAT FIRES EVERY TIME IS WALLPAPER, AND THIS ONE DID. Comparing the two
 * strings exactly meant the console announced an assumption over sentences that were
 * word-for-word identical, because the two fields come back from a model and differ by
 * capitals on a proper noun, a stray comma, or a trailing full stop. Told about an
 * assumption on every single command, an operator stops reading the line, and then misses
 * the one time it says something real.
 *
 * 🔑 SO THE TEST IS THE WORDS, NOT THE CHARACTERS. Case, punctuation and runs of
 * whitespace are stripped from both sides before comparing, which leaves exactly the
 * difference worth announcing: a word that was swapped, added or dropped. "hide everything
 * except for unknowns" against "hide everything except UNKNOWN 01, UNKNOWN 02" still
 * differs and still speaks up. "mesh status" against "Mesh status." does not.
 *
 * ⚠️ DIGITS ARE NOT PUNCTUATION AND ARE DELIBERATELY KEPT. "daymark oh three" becoming
 * "Daymark 03" is a real substitution, and it is one of the two the vocabulary hint exists
 * to make, so it has to survive this comparison.
 */
function meaningfullyDifferent(heard: string, running: string): boolean {
  const bare = (s: string) =>
    s
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  return bare(heard) !== bare(running);
}

export function CommandBar() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  /**
   * Whether to show a pending line where the answer will appear.
   *
   * 🔑 DELAYED, NOT IMMEDIATE, AND THE DELAY IS THE WHOLE DESIGN. Tier 1 answers in about
   * 1.5 seconds and needs nothing: a placeholder that appears and is replaced in the same
   * breath is a flicker, which reads worse than a short wait. Tier 2 takes 12 to 22
   * seconds, and for that long an unchanged screen is indistinguishable from a command
   * that was dropped. So the line waits out the tier-1 case and then says what is
   * happening.
   *
   * ⚠️ THE CLIENT CANNOT KNOW WHICH TIER WILL ANSWER. It is one request, and the tier comes
   * back with the reply, so the elapsed time is the only signal available before then.
   */
  const [waiting, setWaiting] = useState(false);
  const activityRef = useRef<HTMLDivElement>(null);
  const [recording, setRecording] = useState(false);

  /**
   * The command reference, opened by the two gestures that both mean "I am about to give a
   * command": putting the cursor in the box, and pressing the microphone.
   *
   * ⚠️ `muted` SUPPRESSES THE AUTOMATIC OPENING ONLY. The card is still reachable from the
   * ? button, which is why the control says "don't open automatically" rather than
   * "disable": one click that hid a reference permanently, with no way back, would be a
   * trap rather than a preference.
   */
  const [helperOpen, setHelperOpen] = useState(false);
  const [muted, setMutedState] = useState(helperMuted);
  const [micError, setMicError] = useState<string | null>(null);
  /** Live input level, 0 to 1. Shown while recording so "it is listening" is visible. */
  const [level, setLevel] = useState(0);

  const assets = useStore((s) => s.assets);
  const log = useStore((s) => s.commandLog);
  const append = useStore((s) => s.appendCommand);
  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const select = useStore((s) => s.select);
  const setCamera = useStore((s) => s.setCamera);
  const setTrack = useStore((s) => s.setTrack);
  const setHighlight = useStore((s) => s.setHighlight);
  const setShowIce = useStore((s) => s.setShowIce);
  const setHiddenKinds = useStore((s) => s.setHiddenKinds);
  const pushRecent = useStore((s) => s.pushRecent);

  /**
   * The open question, if the last command was ambiguous.
   *
   * Local state rather than the store: it belongs to this exchange and dies with it, and
   * putting it in the store would invite something else to render a stale copy.
   */
  const [clarify, setClarify] = useState<Clarify | null>(null);
  /**
   * Is the whole exchange shown, or only the latest one?
   *
   * 🔑 EXPANDED BY DEFAULT, because the transcript is how the two tiers are seen working.
   * Collapsing is for the moment the map matters more than the conversation, which on a
   * display this size is most of the time once a few commands have run.
   */
  const [expanded, setExpanded] = useState(true);

  /** Which transcript line has its reasoning expanded. One at a time, collapsed by default. */
  const [open, setOpen] = useState<number | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  // 🔑 THE TRANSCRIPT FOLLOWS THE NEWEST LINE. It is capped and scrolls, so without this an
  // answer arriving below the fold looks exactly like no answer arriving: the operator is
  // reading the top of a list while the reply lands out of sight. Keyed on the pending line
  // as well as the log, because that appears and disappears without the log changing.
  //
  // ⚠️ `expanded` IS IN THE LIST FOR A REASON THAT IS NOT OBVIOUS. Opening the transcript
  // restores lines ABOVE the ones already showing, so the scroll position that was at the
  // bottom of a short list is now partway up a long one, and the newest answer, the only
  // one the operator was actually reading, is pushed out of sight by the history they just
  // asked to see. Expanding has to land at the bottom.
  useEffect(() => {
    const el = activityRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log, waiting, expanded]);
  const recorder = useRef<MediaRecorder | null>(null);

  // 🔑 THE FIELD IS NOT FOCUSED ON LOAD, AND THE REFERENCE CARD IS WHY. It used to be: this
  // is the primary interface, so landing with the cursor in it saved a click. Once the card
  // existed that became inconsistent, because the two states disagreed about what was
  // happening. The cursor said "you are typing a command"; the card, which opens on exactly
  // that intent, said nothing. One of them had to be wrong, and it is cheaper to lose a
  // click than to have the console assert two different things about the same moment.
  //
  // So the page lands inert: typing does nothing until the operator says so by clicking in,
  // and when they do, both the cursor and the card agree.

  async function run(
    utterance: string,
    source: "typed" | "voice" | "ui_button",
    opts: { parentCommandId?: string; plan?: unknown[]; heard?: string } = {},
  ) {
    if ((!utterance.trim() && !opts.plan) || busy) return;
    setBusy(true);
    const slow = window.setTimeout(() => setWaiting(true), THINKING_AFTER_MS);
    // 🔴 THE OPERATOR'S LINE IS WHAT THE OPERATOR SAID. For a spoken command the words that
    // get RUN may differ from the words that were HEARD, because names are matched to what
    // is on the map, and showing the corrected sentence as though it were theirs is the
    // worst of both: they cannot tell a microphone problem from a decision the system made,
    // and they have no way to correct something they were never shown.
    //
    // ⚠️ IT IS NOT HYPOTHETICAL. "hide everything except for unknowns" came back on screen
    // as "hide everything except UNKNOWN 01, UNKNOWN 02, UNKNOWN 03", three names nobody
    // said, because the vocabulary hint is a list of live asset names.
    const said = opts.heard ?? utterance;
    // A chip press is the operator answering, not a new utterance, so it is logged as
    // their line. Without it the transcript reads as though the system talked to itself.
    append({ role: "user", text: said, source: source === "voice" ? "voice" : "typed" });
    // 🔑 AN ASSUMPTION IS STATED, NEVER SILENT. When the words that will run differ from
    // the words that were heard, the console says which names it matched before the answer
    // arrives, so the correction is visible at the moment it is made rather than inferred
    // afterwards from an answer that looks wrong.
    if (opts.heard && meaningfullyDifferent(opts.heard, utterance)) {
      append({ role: "system", text: `Assuming you meant: ${utterance}`, ok: true });
    }
    // The question is answered the moment it is acted on, so the chips go now rather than
    // lingering under the reply to themselves.
    setClarify(null);

    try {
      const { bbox, selectedId, recent } = useStore.getState();
      const res = await fetch("/api/command", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          ...(opts.plan ? { plan: opts.plan } : { utterance }),
          source,
          ...(opts.parentCommandId ? { parent_command_id: opts.parentCommandId } : {}),
          // 🔑 THE ORIGINAL WORDS TRAVEL WITH THE CORRECTED ONES. The transcriber matched a
          // spoken name against the map using sound alone; tier 2 can see the whole world
          // and the request, so it is better placed to judge that guess than the component
          // that made it. Sent only when there is a difference to judge.
          ...(opts.heard && opts.heard !== utterance ? { heard: opts.heard } : {}),
          // The deixis carrier. Without it "what is in the current window" and
          // "focus this" are both unanswerable, and people say those constantly.
          // ⚠️ `selected_id` is now LOAD-BEARING: the executor resolves "this asset" from
          // it, so a stale selection produces an honest refusal that looks like a bug.
          // 🔑 `recent` is what makes "list them" mean the three things just shown rather
          // than the whole world. Oldest first, newest LAST: the server binds "them" to the
          // last entry's ids, so the order is part of the contract.
          context: { bbox, selected_id: selectedId, recent },
        }),
      });
      // 🔴 A TRANSPORT FAILURE IS STILL A LINE THE OPERATOR READS. This used to throw and
      // let the catch below print `String(e)`, so a bad response put "Error: command
      // failed: 503" in the history: a status code, the word Error, and the shape of a
      // stack trace, in the one place on screen reserved for answers.
      //
      // 🔑 THE SERVER'S OWN SENTENCE FIRST. The API answers a database outage with a
      // written explanation, so when there is one it is better than anything this side can
      // invent. `detail` is only trusted when it is a string: a 422 from request
      // validation carries a list of objects, which is a developer's artifact and must
      // never be rendered.
      if (!res.ok) {
        const said = await res
          .json()
          .then((b) => (typeof b?.detail === "string" ? b.detail : ""))
          .catch(() => "");
        append({ role: "system", text: said || UNREACHABLE, ok: false });
        return;
      }
      const body = (await res.json()) as CommandResponse;

      const fx = body.ui_effects ?? {};

      // 🔴 BRANCH ON `clarify` BEFORE `ok`. A clarification comes back with `ok: false`,
      // which is honest because the command genuinely did not run, but styling it as an
      // error puts a red failure line above a row of buttons and reads as broken.
      const asking = fx.clarify !== undefined;
      append({
        role: "system",
        text: body.summary,
        tier: body.tier ?? undefined,
        ok: asking ? true : body.ok,
        thinking: thinkingOf(body),
        escalatedFrom: body.escalated_from,
      });
      if (asking) setClarify(fx.clarify!);

      // 🔑 THE OTHER HALF OF THE REFERENCE CARD, AND THE HALF THAT STICKS. The card teaches
      // before, when somebody thinks to look; this teaches at the moment the lesson has a
      // cost attached, which is when a person actually remembers it. Both are built from
      // one canonical phrasing per tool, so they cannot disagree.
      //
      // ⚠️ SUCCESSES ONLY. After a refusal the operator has a problem to solve, and a note
      // about a cheaper way to have failed is noise.
      if (body.ok && !asking && body.teach?.length) {
        const says = body.teach.map((x) => `"${x.say}"`).join(", ");
        append({
          role: "system",
          text: `Reachable without the model: ${says}`,
          ok: true,
          hint: true,
        });
      }

      // 🔒 BUILT FROM THE RESPONSE, NEVER FROM WHAT WAS SENT. The plan that ran is the
      // resolved one and may differ from what was said, and the ids that came back are the
      // only true record of what "them" now refers to. Successes only: a refusal is not a
      // thing a pronoun can point at.
      if (body.ok && !asking) {
        const ids = [
          ...new Set((body.results ?? []).flatMap((r) => r.data?.ids ?? [])),
        ].slice(0, RECENT_IDS);
        pushRecent({ utterance, summary: body.summary, tier: body.tier, ids });
      }

      if (fx.camera) setCamera(fx.camera);
      if (fx.select !== undefined) select(fx.select);
      // Unconditional, including when the key is absent: a trail belongs to the question
      // that produced it, so the next command clears it rather than leaving the previous
      // answer drawn under the new one.
      setTrack(fx.track ?? null);
      // ⚠️ `undefined` and `[]` mean different things: leave it alone, versus clear it.
      if (fx.highlight !== undefined) setHighlight(fx.highlight);
      if (fx.overlay) setShowIce(fx.overlay.visible);

      // 🔑 RESOLVED AGAINST THE CURRENT SET HERE, because this side is the only one that
      // has it. `only` and `all` replace the set outright, which is what makes them mean
      // the same thing regardless of what was already switched off; `hide` and `show`
      // are the two that fold into what is there.
      if (fx.kinds) {
        const named = fx.kinds.kinds;
        setHiddenKinds(
          fx.kinds.mode === "all"
            ? []
            : fx.kinds.mode === "only"
              ? ALL_KINDS.filter((k) => !named.includes(k))
              : fx.kinds.mode === "hide"
                ? [...new Set([...useStore.getState().hiddenKinds, ...named])]
                : useStore.getState().hiddenKinds.filter((k: AssetKind) => !named.includes(k)),
        );
      }

      // Anything may have changed the world, so the world is refetched rather than
      // patched locally. Patching would mean the client owning a second model of state
      // that can disagree with the database, which is the bug this avoids by construction.
      // A clarification changed nothing, so there is nothing to refetch.
      if ((body.ok || fx.refetch) && !asking) {
        fetchAssets().then(setAssets).catch(() => {});
        fetchMesh().then(setMesh).catch(() => {});
      }
    } catch (e) {
      // ⚠️ WHAT REACHES HERE IS A BROKEN CONNECTION, NOT A REFUSAL: the fetch never
      // completed, or the body was not JSON. `String(e)` on that is "TypeError: Failed to
      // fetch", which tells an operator nothing and reads like the page is broken. The
      // real error goes to the browser console, where the person who wants it is looking.
      console.error(e);
      append({ role: "system", text: UNREACHABLE, ok: false });
    } finally {
      window.clearTimeout(slow);
      setWaiting(false);
      setBusy(false);
    }
  }

  /**
   * Record, then post the audio for transcription and run whatever comes back.
   *
   * ⚠️ THE AUDIO LEAVES THE BROWSER. It is sent to a cloud model, and the README says so
   * plainly rather than letting "local-feeling" UI imply otherwise. Everything else in
   * this app is deliberately free of runtime network calls; this one is not, and
   * pretending otherwise would be exactly the kind of claim this project keeps deleting.
   */
  async function toggleMic() {
    setMicError(null);
    if (recording) {
      recorder.current?.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setMicError("this browser has no microphone API");
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setMicError("microphone permission denied");
      return;
    }

    const chunks: Blob[] = [];
    const mr = new MediaRecorder(stream);
    recorder.current = mr;
    mr.ondataavailable = (e) => e.data.size > 0 && chunks.push(e.data);

    // ---- stop when they stop talking -------------------------------------
    //
    // 🔴 THE TIMER IS NOT ARMED UNTIL SPEECH HAS BEEN HEARD, and that is the whole trick.
    // A naive silence timer fires before the operator has started, because the moment after
    // you press the button is silent by definition. So it waits for real speech first, and
    // only then starts counting quiet.
    //
    // 1.5 seconds of quiet, which suits short imperative commands: these are orders, not
    // dictation, and a longer hold makes every command feel like it ends late.
    const SILENCE_RMS = 0.015; // about 1.5% of full scale
    const SILENCE_MS = 1500;
    const MIN_SPEECH_MS = 300; // a click or a breath is not speech
    const HARD_CAP_MS = 15000; // a stuck microphone must never upload a podcast

    const audio = new AudioContext();
    const analyser = audio.createAnalyser();
    analyser.fftSize = 2048;
    audio.createMediaStreamSource(stream).connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);

    let spoke = false;
    let quietSince: number | null = null;
    let speechMs = 0;
    const startedAt = performance.now();

    const tick = () => {
      if (mr.state !== "recording") return;
      analyser.getByteTimeDomainData(buf);
      // ⚠️ Centred on 128, so it has to be shifted before squaring. Skipping that measures
      // the offset rather than the signal and never falls below the threshold.
      let sum = 0;
      for (const v of buf) {
        const x = (v - 128) / 128;
        sum += x * x;
      }
      const rms = Math.sqrt(sum / buf.length);
      setLevel(Math.min(1, rms * 8));
      const now = performance.now();

      if (rms > SILENCE_RMS) {
        speechMs += 50;
        if (speechMs >= MIN_SPEECH_MS) spoke = true;
        quietSince = null;
      } else if (spoke) {
        quietSince ??= now;
        if (now - quietSince >= SILENCE_MS) {
          mr.stop();
          return;
        }
      }

      if (now - startedAt >= HARD_CAP_MS) {
        mr.stop();
        return;
      }
      setTimeout(tick, 50);
    };

    mr.onstop = async () => {
      // Release the device immediately. A tab holding the mic open shows a recording
      // indicator long after it has stopped listening, which is its own kind of lie.
      // ⚠️ The AudioContext counts as holding it: closing the tracks and leaving the
      // context open keeps the browser's recording indicator lit.
      stream.getTracks().forEach((t) => t.stop());
      void audio.close().catch(() => {});
      setRecording(false);
      setLevel(0);
      const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
      if (blob.size < 1200) {
        setMicError("nothing recorded");
        return;
      }
      // 🔴 SILENCE IS NEVER SENT, BECAUSE A MODEL ASKED TO FIND WORDS IN IT WILL FIND SOME.
      // Pressing record, waiting a second and pressing stop produced "hide everything except
      // FLS Yellowknife": a real asset, a real command shape, and nothing anybody said. The
      // transcription prompt carries the live asset names to make real names transcribe
      // correctly, and that same list is what makes an invented command plausible rather than
      // gibberish, which is far worse. Gibberish gets laughed at; a plausible order gets run.
      //
      // 🔑 IT REUSES THE DETECTOR THAT IS ALREADY RUNNING. `spoke` turns true only after
      // MIN_SPEECH_MS above SILENCE_RMS, which is the same judgement the automatic stop is
      // built on, so there is one definition of speech here rather than two that can drift.
      // The byte-count guard above cannot do this job: a second of near-silence is plenty of
      // bytes.
      //
      // ⚠️ IT SAYS NOTHING, DELIBERATELY. Somebody who did not speak does not need to be
      // told they did not speak, and the level meter beside the button is already showing
      // them whether the microphone is hearing anything, which is the only case where the
      // silence is a surprise. A notice here would be the console remarking on a non-event.
      if (!spoke) return;
      setBusy(true);
      try {
        const res = await fetch("/api/transcribe", {
          method: "POST",
          headers: { "content-type": blob.type },
          body: blob,
        });
        const body = await res.json();
        if (!res.ok || !body.text) {
          // ⚠️ SILENCE IS NOT AN ERROR AND IS NOT REPORTED. The server flags the case where
          // there was simply nothing to transcribe, which is the one outcome the operator
          // already knows about. Everything else, a missing key, a format the model will not
          // take, a provider refusing, is worth saying out loud, because the microphone
          // looks identical from the outside in all of them.
          if (!body.silent) {
            setMicError(body.detail || body.summary || "transcription unavailable");
          }
          return;
        }
        setText(body.text);
        setBusy(false);
        // 🔑 THE TRANSCRIPTION'S OWN id BECOMES THIS COMMAND'S PARENT, which is what makes a
        // spoken command ONE thread in the audit log instead of two unrelated rows: "some
        // audio became these words" and, separately, "this command ran". The first question
        // anyone asks of a voice interface after it does something surprising is what the
        // person actually said, and that has to be answerable from the log.
        await run(body.text, "voice", {
          parentCommandId: body.command_id,
          heard: typeof body.heard === "string" ? body.heard : undefined,
        });
        setText("");
        return;
      } catch (e) {
        setMicError(String(e));
      } finally {
        setBusy(false);
      }
    };
    mr.start();
    setRecording(true);
    tick();
  }

  // ---- let the map send commands through this bar ------------------------
  //
  // 🔑 SO A PLACEMENT MADE BY HAND ENDS UP EXACTLY WHERE A SPOKEN ONE DOES: in the
  // transcript, behind the same `busy` flag, and in the audit log. The map has the position
  // and nothing else; this has the sending and nothing else.
  //
  // ⚠️ THROUGH A REF, BECAUSE `run` IS A NEW FUNCTION EVERY RENDER. Registering `run`
  // itself would either re-register on each render or capture the first one forever, and
  // the first one closes over stale state. The wrapper is stable, what it calls is current.
  const runRef = useRef(run);
  // Refreshed in an effect rather than during render, which is the rule: a render may be
  // discarded, and a ref written by one that was would point at a function from a tree
  // that never existed.
  useEffect(() => {
    runRef.current = run;
  });
  useEffect(() => {
    setCommandRunner((utterance, source, opts) => {
      void runRef.current(utterance, source, opts);
    });
    return () => setCommandRunner(null);
  }, []);

  return (
    <div className="cmdwrap">
      {/* 🔑 THE ARROW POINTS THE WAY THE PANEL WILL MOVE, not at the state it is in. Down
          means "this comes down to one line", up means "this opens back up". The other
          convention, pointing at the current state, reads as a label rather than a control
          and leaves you working out which way it will go before you press it.

          ⚠️ ONLY WHEN THERE IS SOMETHING TO COLLAPSE. A toggle over a single line is a
          control that does nothing, and one over an empty transcript is worse. */}
      {log.length > 1 && (
        <button
          className="acollapse"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? "collapse command history" : "expand command history"}
          title={expanded ? "show only the latest command" : "show the whole exchange"}
        >
          {expanded ? "▾" : "▴"}
        </button>
      )}
      {(log.length > 0 || waiting) && (
        <div
          className={`activity${expanded ? "" : " collapsed"}`}
          ref={activityRef}
        >
          {(expanded ? log : log.slice(-LATEST_EXCHANGE)).map((e, i) => (
            <div
              key={i}
              className={`entry ${e.role}${e.ok === false ? " bad" : ""}${e.hint ? " hint" : ""}`}
            >
              <span className="marker">{e.role === "user" ? (e.source === "voice" ? "🎤" : "›") : "·"}</span>
              <span className="etext">
                {e.text}
                {/* 🔴 THE ESCALATION NOTE IS GONE FROM THE ANSWER LINE, DELIBERATELY. It read
                    "the parser could not resolve that, so the model was asked", which is the
                    machinery narrating its own internals directly above the answer, and it
                    arrived attached to replies that had SUCCEEDED. The operator asked what a
                    survey was; being told a component failed is not part of the answer.

                    ⚠️ NOT DELETED, MOVED. Which tier answered is still on screen, in the tier
                    chip beside the line, and the full escalation story with its reasons is in
                    the audit log where a story belongs. Nothing about the two tiers being real
                    and separately observable is lost; it just stops interrupting. */}
                {/* Collapsed by default and dimmer than the answer, so it never competes
                    with it. ⚠️ It is an EXPLANATION, never the answer: the summary above is
                    the executor's account of what ran, this is an account of what was
                    intended, and the two diverge exactly when a plan half-failed. */}
                {e.thinking && open === i && <span className="thinking">{e.thinking}</span>}
              </span>
              {e.tier && (
                // The tier chip is where the reasoning hangs, because the chip is already
                // the thing on screen that says which half of the system answered.
                <button
                  className={`tier ${e.tier}${e.thinking ? " has" : ""}`}
                  onClick={() => e.thinking && setOpen(open === i ? null : i)}
                  title={e.thinking ? "Why this answer" : undefined}
                  aria-expanded={e.thinking ? open === i : undefined}
                >
                  {e.tier}
                </button>
              )}
            </div>
          ))}

          {/* Where the answer will appear. Shown only once a command has run long enough to
              be worth explaining, so the fast path never sees it. */}
          {waiting && (
            <div className="entry system pending" aria-live="polite">
              <span className="marker">·</span>
              <span className="etext">Thinking…</span>
            </div>
          )}
        </div>
      )}

      {/* 🥇 THE AMBIGUITY BEAT. An ambiguous command comes back with candidates instead of a
          guess, the operator picks, and the audit log shows the chain through
          `parent_command_id`. Answering is an ordinary command post carrying the option's
          own plan, not a second endpoint. */}
      {clarify && (
        <div className="clarify">
          <div className="cq">{clarify.question}</div>
          <div className="chips">
            {clarify.options.map((o) =>
              // ⚠️ `plan: null` is a real case and means the vague phrase could not be
              // substituted. A button there would re-ask the same question forever, so
              // those are shown as plain text: still useful to read, not pressable.
              o.plan ? (
                <button
                  key={o.id}
                  className="chip"
                  disabled={busy}
                  onClick={() =>
                    void run(o.label, "ui_button", {
                      plan: o.plan!,
                      parentCommandId: clarify.command_id,
                    })
                  }
                >
                  <span className="clabel">{o.label}</span>
                  <span className="cdetail">{o.detail}</span>
                </button>
              ) : (
                <span key={o.id} className="chip flat">
                  <span className="clabel">{o.label}</span>
                  <span className="cdetail">{o.detail}</span>
                </span>
              ),
            )}
          </div>
          {/* Saying how many were left out beats silently truncating: the operator can see
              that narrowing the phrase is worth doing. */}
          {clarify.total > clarify.options.length && (
            <div className="cmore">
              and {clarify.total - clarify.options.length} more, narrow it down
            </div>
          )}
        </div>
      )}

      {/* 🔑 ONE FOCUS REGION, INPUT AND CARD TOGETHER, and this is the whole trick. React's
          onFocus and onBlur are focusin and focusout, so they bubble: the wrapper sees
          focus enter and leave the group as a unit. Closing on the input's own blur would
          mean clicking the card blurs the input and closes the card out from under the
          click, which is the classic version of this bug. `relatedTarget` is where focus
          went next; if it is still inside this element, nothing has actually left. */}
      <div
        className="cmdgroup"
        onFocus={() => {
          if (!muted) setHelperOpen(true);
        }}
        onBlur={(ev) => {
          if (!ev.currentTarget.contains(ev.relatedTarget as Node | null)) setHelperOpen(false);
        }}
      >
        <HelperSheet
          open={helperOpen}
          assets={assets}
          onMute={() => {
            setMuted(true);
            setMutedState(true);
            setHelperOpen(false);
          }}
        />

      <form
        className="cmdbar"
        onSubmit={(ev) => {
          ev.preventDefault();
          const t = text;
          setText("");
          // The answer deserves the space, and the line under it that names the tier-1
          // phrasing is what carries the teaching from here.
          setHelperOpen(false);
          void run(t, "typed");
        }}
      >
        <button
          type="button"
          className={`mic${recording ? " live" : ""}`}
          onClick={() => {
            // Pressing the microphone is the same intent as clicking into the box, and it
            // is the case the card matters most for: you can read it while you speak.
            if (!muted && !recording) setHelperOpen(true);
            void toggleMic();
          }}
          disabled={busy && !recording}
          title={recording ? "Stop and send" : "Speak a command. Audio is sent to a cloud model"}
          aria-label={recording ? "Stop recording" : "Record a spoken command"}
        >
          {recording ? "◼" : "🎤"}
        </button>

        {/* Showing the level is what makes an automatic stop read as deliberate rather than
            abrupt: you can see it listening, so you can see why it stopped. Driven by the
            same rms the silence detector uses, so it costs nothing extra. */}
        {recording && (
          <span className="meter" aria-hidden="true">
            <span className="meterfill" style={{ transform: `scaleX(${level})` }} />
          </span>
        )}

        <input
          ref={inputRef}
          className="cmdinput"
          value={text}
          /* 🔑 THE SAME CEILING THE SERVER ENFORCES, SO NOBODY MEETS IT AS AN ERROR. The
             request model refuses anything longer, which is the guard that matters because
             the endpoint is public. Repeating it here means a long paste is simply cut at
             the box instead of coming back as a validation failure the operator has to
             interpret. ⚠️ Not a substitute for the server's cap: this one is advice to a
             browser, and the browser is not the only caller. */
          maxLength={1000}
          disabled={busy}
          /* ⚠️ THE ROTATING EXAMPLE THAT USED TO LIVE HERE IS GONE. It cycled four
             suggestions through this box, which is the same job the reference card does at
             the same moment and does properly: four hand-written phrasings that nothing
             checked, against every phrasing the parser answers, verified by the suite. Two
             teaching mechanisms competing in one square inch, and this was the weaker. */
          placeholder={busy ? "working…" : "type a command, or press ? for the list"}
          onChange={(e) => setText(e.target.value)}
          /* 🔴 CLICK AS WELL AS FOCUS, AND THE PAGE LANDS IN EXACTLY THE STATE THAT NEEDS
             IT. The field is focused on load, so the first click on it moves focus from
             nowhere to nowhere and fires no focus event at all: the operator clicks the
             box they were told to use and nothing happens. Focus covers tabbing in, this
             covers clicking a field that already has the cursor. */
          onClick={() => {
            if (!muted) setHelperOpen(true);
          }}
          aria-label="Command"
        />

        <button type="submit" disabled={busy || !text.trim()}>
          {busy ? "…" : "RUN"}
        </button>

        {/* ⚠️ THE WAY BACK, and it is why the mute control can be one click. A preference
            that hides something with no visible route to it is not a preference. */}
        <button
          type="button"
          className="helperopen"
          /* ⚠️ FORCES OPEN RATHER THAN TOGGLING. Focusing this button also fires the
             group's focus handler, so a toggle read the state the focus had just set and
             immediately undid it: pressing ? closed the card it had opened, which looked
             like the button was broken. Its real job is the muted case, where nothing else
             opens the card; closing is what blur and submit are for. */
          onClick={() => setHelperOpen(true)}
          aria-expanded={helperOpen}
          title="What can I say?"
          aria-label="Show the command reference"
        >
          ?
        </button>
      </form>
      </div>

      {micError && <div className="micerr">{micError}</div>}
    </div>
  );
}
