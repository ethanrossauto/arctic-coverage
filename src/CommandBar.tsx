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

import { fetchAssets, fetchMesh } from "./assets";
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
    /** Tier 1: which branch matched, and what it took out of the sentence. */
    matched?: string;
    extracted?: Record<string, unknown>;
    /**
     * 🔑 THE MOST VALUABLE FIELD HERE. Words the parser threw away. Two utterances
     * differing only by "on foot" returned identical answers because the phrase was
     * silently dropped, and on a misheard voice command an invented asset name shows up
     * as an extracted parameter nobody asked for. Both are invisible without this.
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
    lines.push(
      `parser: dropped ${p.ignored.map((w) => `"${w}"`).join(", ")} → asked the model`,
    );
  } else if (p?.matched) {
    lines.push(`parser: matched ${p.matched} → asked the model`);
  }
  if (p?.extracted && Object.keys(p.extracted).length) {
    lines.push(`  parser read: ${paramList(p.extracted)}`);
  }

  if (t.reasoning) lines.push(`${p ? "model:  " : ""}${t.reasoning}`);
  if (t.matched) lines.push(`matched: ${t.matched}`);
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

/** Rotated through the placeholder, so the field suggests what this thing can do. */
const EXAMPLES = [
  "mesh status",
  "show me the drones",
  "what is not broadcasting",
  "which assets are overdue",
];

export function CommandBar() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  /** Live input level, 0 to 1. Shown while recording so "it is listening" is visible. */
  const [level, setLevel] = useState(0);

  const log = useStore((s) => s.commandLog);
  const append = useStore((s) => s.appendCommand);
  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const select = useStore((s) => s.select);
  const setCamera = useStore((s) => s.setCamera);
  const setTrack = useStore((s) => s.setTrack);
  const setHighlight = useStore((s) => s.setHighlight);
  const setShowIce = useStore((s) => s.setShowIce);
  const pushRecent = useStore((s) => s.pushRecent);

  /**
   * The open question, if the last command was ambiguous.
   *
   * Local state rather than the store: it belongs to this exchange and dies with it, and
   * putting it in the store would invite something else to render a stale copy.
   */
  const [clarify, setClarify] = useState<Clarify | null>(null);

  /** Which transcript line has its reasoning expanded. One at a time, collapsed by default. */
  const [open, setOpen] = useState<number | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);

  // Focus the field on load. This is the primary interface, so landing with the cursor
  // anywhere else would be asking the operator to hunt for it.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function run(
    utterance: string,
    source: "typed" | "voice" | "ui_button",
    opts: { parentCommandId?: string; plan?: unknown[] } = {},
  ) {
    if ((!utterance.trim() && !opts.plan) || busy) return;
    setBusy(true);
    // A chip press is the operator answering, not a new utterance, so it is logged as
    // their line. Without it the transcript reads as though the system talked to itself.
    append({ role: "user", text: utterance, source: source === "voice" ? "voice" : "typed" });
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
      if (!res.ok) throw new Error(`command failed: ${res.status}`);
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

      // Anything may have changed the world, so the world is refetched rather than
      // patched locally. Patching would mean the client owning a second model of state
      // that can disagree with the database, which is the bug this avoids by construction.
      // A clarification changed nothing, so there is nothing to refetch.
      if ((body.ok || fx.refetch) && !asking) {
        fetchAssets().then(setAssets).catch(() => {});
        fetchMesh().then(setMesh).catch(() => {});
      }
    } catch (e) {
      append({ role: "system", text: String(e), ok: false });
    } finally {
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
      setBusy(true);
      try {
        const res = await fetch("/api/transcribe", {
          method: "POST",
          headers: { "content-type": blob.type },
          body: blob,
        });
        const body = await res.json();
        if (!res.ok || !body.text) {
          setMicError(body.detail || body.summary || "transcription unavailable");
          return;
        }
        setText(body.text);
        setBusy(false);
        // 🔑 THE TRANSCRIPTION'S OWN id BECOMES THIS COMMAND'S PARENT, which is what makes a
        // spoken command ONE thread in the audit log instead of two unrelated rows: "some
        // audio became these words" and, separately, "this command ran". The first question
        // anyone asks of a voice interface after it does something surprising is what the
        // person actually said, and that has to be answerable from the log.
        await run(body.text, "voice", { parentCommandId: body.command_id });
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

  return (
    <div className="cmdwrap">
      {log.length > 0 && (
        <div className="activity">
          {log.map((e, i) => (
            <div key={i} className={`entry ${e.role}${e.ok === false ? " bad" : ""}`}>
              <span className="marker">{e.role === "user" ? (e.source === "voice" ? "🎤" : "›") : "·"}</span>
              <span className="etext">
                {e.text}
                {/* 🔑 "I did not recognise that, so I asked the model." The clearest thing on
                    screen showing the two tiers are real and that the expensive one runs
                    only when it earns it. ⛔ NOT an error state: these responses succeeded. */}
                {e.escalatedFrom && (
                  <span className="escalated">
                    the {e.escalatedFrom} could not resolve that, so the model was asked
                  </span>
                )}
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

      <form
        className="cmdbar"
        onSubmit={(ev) => {
          ev.preventDefault();
          const t = text;
          setText("");
          void run(t, "typed");
        }}
      >
        <button
          type="button"
          className={`mic${recording ? " live" : ""}`}
          onClick={() => void toggleMic()}
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
          disabled={busy}
          placeholder={busy ? "working…" : `try: ${EXAMPLES[log.length % EXAMPLES.length]}`}
          onChange={(e) => setText(e.target.value)}
          aria-label="Command"
        />

        <button type="submit" disabled={busy || !text.trim()}>
          {busy ? "…" : "RUN"}
        </button>
      </form>

      {micError && <div className="micerr">{micError}</div>}
    </div>
  );
}
