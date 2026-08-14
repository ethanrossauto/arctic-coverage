/**
 * The command reference: what an operator can say, and what is on the map right now.
 *
 * 🔑 WHY THIS EXISTS AT ALL. The deterministic tier answers about thirty phrasings
 * instantly and for nothing, and keeps working when the model is unreachable. None of that
 * is any use to a person who cannot find out what those phrasings are. Without a reference
 * tier 1 is a cache that happens to answer sometimes; with one it is a control surface an
 * operator can drive on purpose, which is the difference between an optimisation and a
 * capability.
 *
 * 🔑 WHY A CARD RATHER THAN INLINE COMPLETION. You can read and speak at the same time; you
 * cannot read a dropdown and type at the same time in any useful way. A `/`-triggered
 * palette was considered first and rejected as the primary, because it serves typing only
 * and voice is a first-class input here. Aircraft carry a quick-reference handbook for the
 * same reason.
 *
 * 🔑 AND THE PHRASINGS ARE THE SAME LIST THE PARSER ANSWERS. They come from the tool
 * registry over `/api/tools`, and `tests/test_reference.py` runs every one of them through
 * `parser.parse`. A reference that teaches a command the console does not answer is worse
 * than none: the operator learns not to trust it, and then it is dead weight.
 */
import { useEffect, useState } from "react";

import { KIND_LABEL, type Asset, type AssetKind } from "./assets";

/** One heading and the sentences under it, as the server groups them. */
interface RefGroup {
  key: string;
  label: string;
  /**
   * The sentence, the tool it reaches, and what that tool does.
   *
   * 🔑 THE TOOL IS SHOWN BESIDE THE PHRASE because three of these verbs are indistinguishable
   * from the sentences alone. That was literally true while four commands opened with "show";
   * the language gives every tool its own verb now, so the name beside the phrase confirms the
   * choice rather than rescuing it. Still worth printing: an operator scanning for the right
   * command reads the names, not the sentences.
   *
   * ⚠️ `does` IS SEPARATE FROM `tool` AND USED NOT TO BE. The server sent the gloss in the
   * `tool` field, so the card printed "not announcing" in the position a reader takes for a
   * function name. Two fields means the line can say which is which.
   */
  says: { say: string; tool: string; does: string }[];
}

/**
 * Fetched once per page rather than per open. The registry cannot change under a running
 * client, so re-fetching it on every focus would be a request per keystroke-worth of
 * attention for an answer that never moves.
 */
let cached: RefGroup[] | null = null;

/**
 * 🔒 THE PREFERENCE IS PER BROWSER, AND IT HAS TO BE. There is one database and one shared
 * world, so a preference stored server-side would be one visitor turning the card off for
 * everyone, which is the same surprise the reset already has to disclose on the status
 * strip. This is the one piece of state that is genuinely about the person, not the world.
 */
const MUTED_KEY = "arctic.helper.muted";

export function helperMuted(): boolean {
  try {
    return localStorage.getItem(MUTED_KEY) === "1";
  } catch {
    // Private browsing, or storage disabled. Showing the card is the safe default: the
    // cost is a panel somebody has to close, against a reference they cannot reach.
    return false;
  }
}

function setMuted(muted: boolean): void {
  try {
    localStorage.setItem(MUTED_KEY, muted ? "1" : "0");
  } catch {
    /* nothing to do: the card simply keeps opening */
  }
}

export function HelperSheet({
  open,
  assets,
  onMute,
}: {
  open: boolean;
  assets: Asset[];
  onMute: () => void;
}) {
  const [groups, setGroups] = useState<RefGroup[] | null>(cached);

  useEffect(() => {
    if (!open || cached) return;
    let live = true;
    void fetch("/api/tools")
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        const ref = body?.reference as RefGroup[] | undefined;
        if (!ref || !live) return;
        cached = ref;
        setGroups(ref);
      })
      // A reference that fails to load leaves the console exactly as usable as it was
      // before there was one. It is not worth an error message.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [open]);

  // 🔑 WHAT IS ACTUALLY OUT THERE, NOT WHAT THE SCHEMA ALLOWS. The kinds are counted from
  // the live world, so the card names the nouns an operator can currently say and leaves
  // out the ones that would return nothing. It earns its space twice: reference and status.
  const counts = new Map<AssetKind, number>();
  for (const a of assets) counts.set(a.kind, (counts.get(a.kind) ?? 0) + 1);
  const kinds = [...counts.entries()].sort((a, b) => b[1] - a[1]);

  if (!open) return null;

  return (
    // ⚠️ NOT FOCUSABLE ITSELF, and nothing inside it takes focus except the mute button,
    // which is inside the same focus region as the input so clicking it cannot close the
    // card out from under the click.
    <div
      className="helper"
      role="note"
      aria-label="Command reference"
      /* 🔴 THE CARD NEVER TAKES FOCUS, AND THIS IS THE LINE THAT MAKES THE PANEL WORK.
         Closing on focus leaving the group is not enough on its own: clicking a heading or
         a phrase moves focus to `body`, so `relatedTarget` is null, nothing contains it,
         and the card closes out from under the click. Refusing the default on mousedown
         means the click never moves focus at all, so the input keeps it and the card
         stays. Click still fires, so the mute button below works normally. */
      onMouseDown={(ev) => ev.preventDefault()}
    >
      <div className="helperhead">
        <span>COMMANDS</span>
        <button
          type="button"
          className="helpermute"
          onClick={onMute}
          /* Says what it does. The card stays reachable from the ? beside the input, so
             "disable" would overstate it and read as a one-way door. */
          title="Stop the reference opening by itself. The ? beside the input brings it back."
        >
          don&rsquo;t open automatically
        </button>
      </div>

      {/* 🔴 ONE COLUMN, AND IT WAS TWO. The two-column version was a fix for the card
          overflowing its own height, and it worked by making every line narrow enough that
          only a three-word fragment fitted beside the sentence. That traded the thing the
          card is for: a reader in the middle of choosing between two phrasings could see
          neither what the tool was called nor what it did.

          ⚠️ SO THE HEIGHT PROBLEM IS BACK AND IS SOLVED WHERE IT BELONGS, on `max-height`
          in the stylesheet, rather than by folding the content in half. */}
      {groups === null ? (
        <p className="helperwait">loading…</p>
      ) : (
        <div className="helperbody">
          {groups.map((g) => (
            <section key={g.key} className="helpergroup">
              <h4>{g.label}</h4>
              <ul>
                {g.says.map((s) => (
                  <li key={s.say}>
                    <span className="helpersay">{s.say}</span>{" "}
                    <span className="helpertool">
                      ({s.tool}
                      {s.does ? ` - ${s.does}` : ""})
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      {kinds.length > 0 && (
        <section className="helpergroup">
          <h4>ON THE MAP NOW</h4>
          <ul className="helperkinds">
            {kinds.map(([kind, n]) => (
              <li key={kind}>
                {KIND_LABEL[kind] ?? kind} <span className="helpern">{n}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* One line, deliberately. It was two and the second was clipped by the card's own
          height, which is a footnote nobody reads explaining a thing nobody sees. */}
      <p className="helperfoot">Anything else goes to the model: slower, and it costs a little.</p>
    </div>
  );
}

export { setMuted };
