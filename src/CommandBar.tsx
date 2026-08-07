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
import { useStore } from "./store";
import type { CameraTarget } from "./store";

/** What the server may ask the client to do after a plan runs. */
interface UiEffects {
  camera?: CameraTarget;
  select?: string | null;
}

interface CommandResponse {
  ok: boolean;
  summary: string;
  tier: string | null;
  results: { tool: string; ok: boolean; message: string }[];
  ui_effects?: UiEffects;
}

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

  const log = useStore((s) => s.commandLog);
  const append = useStore((s) => s.appendCommand);
  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const select = useStore((s) => s.select);
  const setCamera = useStore((s) => s.setCamera);

  const inputRef = useRef<HTMLInputElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);

  // Focus the field on load. This is the primary interface, so landing with the cursor
  // anywhere else would be asking the operator to hunt for it.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function run(utterance: string, source: "typed" | "voice") {
    if (!utterance.trim() || busy) return;
    setBusy(true);
    append({ role: "user", text: utterance, source });

    try {
      const { bbox, selectedId } = useStore.getState();
      const res = await fetch("/api/command", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          utterance,
          source,
          // The deixis carrier. Without it "what is in the current window" and
          // "focus this" are both unanswerable, and people say those constantly.
          context: { bbox, selected_id: selectedId },
        }),
      });
      if (!res.ok) throw new Error(`command failed: ${res.status}`);
      const body = (await res.json()) as CommandResponse;

      append({
        role: "system",
        text: body.summary,
        tier: body.tier ?? undefined,
        ok: body.ok,
      });

      const fx = body.ui_effects ?? {};
      if (fx.camera) setCamera(fx.camera);
      if (fx.select !== undefined) select(fx.select);

      // Anything may have changed the world, so the world is refetched rather than
      // patched locally. Patching would mean the client owning a second model of state
      // that can disagree with the database, which is the bug this avoids by construction.
      if (body.ok) {
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
    mr.onstop = async () => {
      // Release the device immediately. A tab holding the mic open shows a recording
      // indicator long after it has stopped listening, which is its own kind of lie.
      stream.getTracks().forEach((t) => t.stop());
      setRecording(false);
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
        await run(body.text, "voice");
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
  }

  return (
    <div className="cmdwrap">
      {log.length > 0 && (
        <div className="activity">
          {log.map((e, i) => (
            <div key={i} className={`entry ${e.role}${e.ok === false ? " bad" : ""}`}>
              <span className="marker">{e.role === "user" ? (e.source === "voice" ? "🎤" : "›") : "·"}</span>
              <span className="etext">{e.text}</span>
              {e.tier && <span className={`tier ${e.tier}`}>{e.tier}</span>}
            </div>
          ))}
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
