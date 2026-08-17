/**
 * Client state. One store, holding DOMAIN OBJECTS only.
 *
 * 🔒 Architectural rule, enforced by inspection of this file: nothing here is a
 * MapLibre shape. No GeoJSON, no style-layer config, no source objects. Entities
 * are entities, tracks are point arrays, links are pairs of ids. The map
 * component converts these into whatever its renderer wants, every frame.
 *
 * That is what makes the renderer replaceable, and more immediately it is what
 * lets the same store drive the map, the panels and the command layer without any
 * of them having to know how the others draw.
 *
 * 🔒 THERE IS NO PLAYBACK CLOCK, and that is a decision rather than a gap. The
 * display shows the present: what an asset last reported, what the mesh looks like
 * now, where a contact is. History is a QUERY ("four days of positions for
 * daymark-3"), which returns a series to draw, not a timeline to scrub. A second
 * time control would have to pretend the whole picture can be wound back, and only
 * one layer of it can.
 *
 * The one time control on screen is the ice timebar, and it moves through five years
 * of satellite measurements rather than through the scenario.
 */
import { create } from "zustand";

import type { Asset, AssetKind, IceLayer, MeshStatus } from "./assets";
import type { Phase } from "./session";
import type { WorldStatus } from "./world";
import type { ViewportBbox } from "./map/bounds";

export type Projection = "globe" | "mercator";

/**
 * A position series for one asset, as the server returned it.
 *
 * 🔒 THE ONLY WAY A LINE APPEARS ON THIS MAP. Assets used to draw their seeded route
 * permanently, which meant 11 of the 68 had a line behind them at rest, before anyone had
 * asked anything. That is scenery, and it made the answer to "where has this been" look
 * identical to furniture that was already there. Now a line means somebody asked a
 * question, and it goes away when they ask the next one.
 *
 * Lon-first, GeoJSON order, oldest first, exactly as it arrives.
 */
export interface AssetTrack {
  id: string;
  coordinates: [number, number][];
}

/**
 * Several assets sitting under one click, and where the click was.
 *
 * 🔑 THIS EXISTS BECAUSE THE MAP REFUSES TO HIDE ANYTHING. The icon layers set
 * `icon-allow-overlap: true` on purpose: on a tactical display an asset silently
 * suppressed by collision detection is worse than two overlapping, because the operator
 * cannot tell "not there" from "not drawn". The cost of that decision is that a dense
 * cluster is a pile, and a click on a pile has to pick one.
 *
 * Picking the topmost silently is the wrong answer, because the topmost is decided by
 * draw order rather than by anything the operator can see or predict. So the pile asks.
 */
export interface AssetPick {
  /** Screen coordinates of the click, so the list opens where the finger landed. */
  x: number;
  y: number;
  /** Every asset under the click, nearest the top of the draw order first. */
  ids: string[];
}

/**
 * One turn, kept so the NEXT command can resolve what "them" points at.
 *
 * 🔑 THIS IS DEIXIS, NOT MEMORY, and the distinction is what keeps it cheap. It exists so
 * "list them" binds to the three things just shown rather than to the whole world, which is
 * what happened before it: `how many unknown parties on foot` answered 3, and `list them`
 * answered 76. Three turns is enough for that, and every extra turn is tokens on the next
 * model call for no gain.
 */
export interface RecentTurn {
  utterance: string;
  summary: string;
  tier: string | null;
  /** Ids the answer actually returned. What "them" refers to. */
  ids: string[];
}

/** One line in the on-screen transcript. */
export interface CommandEntry {
  role: "user" | "system";
  text: string;
  /** Which tier answered: the deterministic parser, or the model. Systems lines only. */
  tier?: string;
  ok?: boolean;
  source?: "typed" | "voice";
  /**
   * Why the answer is what it is: the model's reasoning, or the parser's trace.
   *
   * ⚠️ AN EXPLANATION, NEVER THE ANSWER. The summary above is the executor's, written from
   * what actually ran; this is an account of what was intended. They diverge exactly when a
   * plan half-failed, so they are kept apart and shown apart.
   */
  thinking?: string;
  /** The parser tried, could not resolve it, and the model was asked instead. */
  escalatedFrom?: string;
  /**
   * A quiet line teaching the deterministic phrasing that reaches the same tool.
   *
   * Styled down rather than up: it is worth reading once and worth ignoring forever after,
   * so it must not compete with the answer it sits under.
   */
  hint?: boolean;
}

/**
 * Where a command asked the camera to look.
 *
 * Shaped exactly as the executor sends it, lon-first, which is the same order the
 * server stores geometry in and the same order MapLibre wants. Resisting the urge to
 * flip it to lat-first here keeps one convention across the wire and the renderer.
 */
export interface CameraTarget {
  center: [number, number];
  zoom?: number;
  /** How wide the answer was, in km. The reason the zoom is what it is. */
  spread_km?: number;
  /** How many entities the framing was derived from, for the "why did it move" readout. */
  framed?: number;
}

interface State {
  /** Every asset kind, straight from the database. Domain objects, never map shapes. */
  assets: Asset[];
  /**
   * The same assets, carried forward between server fixes so motion is smooth.
   *
   * 🔑 SEPARATE FROM `assets` BECAUSE ONLY ONE OF THEM IS TRUE. `assets` is what the server
   * last said and is what every count, list and answer is computed from. This one is an
   * estimate for drawing, refreshed once a second, and it must never become the input to
   * the next estimate or to anything that reports a number. The map reads this; nothing
   * else should.
   */
  displayAssets: Asset[];
  /** The computed radio link graph. Derived server-side, never stored anywhere. */
  mesh: MeshStatus | null;
  /** The sea ice picture for `iceDate`. Null until the first fetch returns. */
  ice: IceLayer | null;
  /**
   * The date the ice layer is drawn for, as YYYY-MM-DD.
   *
   * 🔑 It moves the ICE and nothing else. Dragging it five years back does not wind the
   * assets back with it, and the readout says which layer it is talking about, because a
   * control that looks like a scenario timeline and is not one is worse than no control.
   *
   * It only ever holds one of the vendored measurement dates. Nothing interpolates between
   * them, because a value between two measurements is a value nobody observed.
   */
  iceDate: string;
  loading: boolean;
  error: string | null;

  /**
   * Whether the console is being driven, or is waiting to be entered.
   *
   * 🔒 IT GATES EVERY POLL, and that is the only reason it lives in the store rather than in
   * the shell's own state. See `session.ts` for the invariant it exists to hold: the database
   * is only ever asleep while this reads `entry`. A timer added later that does not consult
   * this will quietly keep the compute awake for ever, which is the bug this replaced.
   */
  phase: Phase;
  setPhase: (p: Phase) => void;

  /**
   * Set while a request has been outstanding longer than `SLOW_REQUEST_MS`.
   *
   * ⚠️ NOT AN ERROR, AND NOT THE BOOT CURTAIN. It says the console is waiting on something
   * that is taking longer than a warm read should, which is a thing to admit rather than to
   * present as a frozen screen.
   */
  waiting: boolean;
  setWaiting: (v: boolean) => void;

  /**
   * What the PLACE control is set to. `kind` non-null means the map is armed.
   *
   * 🔑 EVERYTHING THE VOICE PATH CAN DO, THE HAND CAN DO. Placing was command-only, so the
   * one action that changes the world had no manual route at all. Arming is two steps on
   * purpose: choose what, then choose where, because a position is the one argument a menu
   * cannot supply and a map click is the natural way to give it.
   *
   * ⚠️ NEVER NULL, AND THAT IS WHY THE FLAGS SIT INSIDE IT. They used to live in a nullable
   * object, so neither could be set until a kind had been chosen and both boxes were greyed
   * out until then. Ordering the operator's decisions for them is not this control's job:
   * ticking UNKNOWN first and picking the kind afterwards is a perfectly ordinary way to
   * think, and the flags outlive a change of kind for the same reason.
   */
  placing: { kind: AssetKind | null; unknown: boolean; backhaul: boolean };

  projection: Projection;
  /**
   * Whether the sea-ice layer is drawn.
   *
   * "Show me the weather overlays" turns this on. 🔒 No server code reads the ice data and
   * none ever will: the command layer asks the display to show a layer it already has,
   * which is why this is client state and not a server fact.
   */
  showIce: boolean;
  /**
   * Whether contacts the console cannot honestly claim are being WITHHELD from the map.
   *
   * 🔒 DEFAULT ON, AND THAT IS THE POINT RATHER THAN A PREFERENCE. The default picture shows
   * what actually reached us. Revealing what it did not has to be a deliberate act, because
   * one of those buckets is a contact nothing is holding, which the console has no honest
   * basis for knowing about at all.
   *
   * 🔑 PHRASED AS "HIDE", NOT "SHOW", AND THE DOUBLE NEGATIVE IS DELIBERATE. A box labelled
   * SHOW that sits unchecked says nothing to a viewer who never ticks it: the display looks
   * complete and quietly is not. A box labelled HIDE that sits checked admits, on the face
   * of the control, that something is deliberately being kept off the map. This display's
   * whole argument is about not claiming more than it can support, and a control that
   * advertises the withholding argues it better than one that merely permits the reveal.
   */
  hideUndetected: boolean;
  /**
   * Kinds the operator has switched off in the VIEW menu. Empty means everything is drawn.
   *
   * 🔑 STORED AS WHAT IS HIDDEN, NOT AS WHAT IS SHOWN, so a kind added to the world later
   * appears by default instead of silently staying off until someone edits this list. The
   * absent case has to be the visible one: a new sensor type that nobody can see, because a
   * settings list predates it, is the kind of bug that looks like missing data.
   *
   * ⚠️ NOT THE SAME QUESTION AS `hideUndetected`, even though both hide things. That one is
   * about what the console may honestly claim; this is a display preference about clutter,
   * and the operator turned it on themselves. Which is why the status strip keeps counting
   * the world rather than the view, and says how many kinds are off instead.
   */
  hiddenKinds: AssetKind[];
  /** Last computed viewport box. What a command means by "the current window". */
  bbox: ViewportBbox | null;
  selectedId: string | null;

  /**
   * What the operator asked and what the executor reported back.
   *
   * 🔒 Display only, and deliberately not the audit log. The real record is the `events`
   * table, written server-side before any effect is visible. This is a transcript of one
   * browser session and is thrown away with the tab; treating it as the record would put
   * the log somewhere a refresh can erase it.
   */
  commandLog: CommandEntry[];
  /**
   * The last few turns, oldest first and NEWEST LAST.
   *
   * ⚠️ ORDER IS LOAD-BEARING: the server binds "them" to the last entry's ids. Successes
   * only, because a refusal is not a thing "them" can point at.
   */
  recent: RecentTurn[];
  /** Where a command asked the camera to go. Null until one does. */
  camera: CameraTarget | null;
  /** The position history a command asked for. Null when nobody has asked. */
  track: AssetTrack | null;
  /** An unresolved click on overlapping assets. Null unless the operator is choosing. */
  picker: AssetPick | null;
  /**
   * Which assets a command's answer points at.
   *
   * 🔑 AN EMPTY ARRAY MEANS CLEAR, NOT "NO CHANGE". Every list query sends its full set
   * each time, so the absence of an id is a statement that it is not in the answer. Treat
   * `[]` as "highlight nothing" and the key being absent as "leave it alone".
   */
  highlightIds: string[];

  /**
   * Where the shared world's idle clock stands, as the server reports it.
   *
   * 🔑 THE WORLD IS SHARED AND THIS IS HOW THE DISPLAY ADMITS IT. One database, one world,
   * every viewer looking at the same thing, so a reset is always global: it lands on
   * everyone, not only on whoever triggered it. That cannot be prevented without giving
   * each visitor their own world, so it is disclosed instead.
   */
  world: WorldStatus | null;
  /**
   * A reset that has just happened and has not been acknowledged.
   *
   * 🔑 KEYED OFF `generation`, NOT OFF A TIMER, which is what makes it cover the case with
   * no warning attached. An idle reset is announced by the countdown beforehand. Another
   * viewer pressing the button is not announced by anything, and from this browser's point
   * of view the two are identical: the world simply changed. One mechanism catches both.
   */
  resetNotice: string | null;
  /** Whether the audit log panel is open. Closed until asked for. */
  auditOpen: boolean;

  setAssets: (a: Asset[]) => void;
  setDisplayAssets: (a: Asset[]) => void;
  setMesh: (m: MeshStatus) => void;
  setIce: (i: IceLayer) => void;
  setIceDate: (d: string) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  setPlacing: (p: { kind: AssetKind | null; unknown: boolean; backhaul: boolean }) => void;
  setProjection: (p: Projection) => void;
  setShowIce: (v: boolean) => void;
  setHideUndetected: (v: boolean) => void;
  toggleKind: (k: AssetKind) => void;
  setHiddenKinds: (k: AssetKind[]) => void;
  setWorld: (w: WorldStatus) => void;
  setResetNotice: (n: string | null) => void;
  setAuditOpen: (v: boolean) => void;
  setBbox: (b: ViewportBbox) => void;
  select: (id: string | null) => void;
  appendCommand: (e: CommandEntry) => void;
  setCamera: (c: CameraTarget | null) => void;
  pushRecent: (t: RecentTurn) => void;
  setTrack: (t: AssetTrack | null) => void;
  setPicker: (p: AssetPick | null) => void;
  setHighlight: (ids: string[]) => void;
}

/** How many transcript lines to keep. Enough to follow a demo, not a scrollback. */
const LOG_LIMIT = 40;

/** How many turns of conversational context travel with a command. */
const RECENT_TURNS = 3;
/** And how many ids one of those turns may carry. */
export const RECENT_IDS = 50;

export const useStore = create<State>((set) => ({
  assets: [],
  displayAssets: [],
  mesh: null,
  ice: null,
  iceDate: new Date().toISOString().slice(0, 10),
  loading: true,
  error: null,
  placing: { kind: null, unknown: false, backhaul: false },
  // Projection can be pinned from the URL (?proj=mercator). Useful for linking
  // someone to a specific view, and it is how the renderer swap gets exercised
  // without a click during automated screenshots.
  projection: (new URLSearchParams(location.search).get("proj") === "mercator"
    ? "mercator"
    : "globe") as Projection,
  showIce: true,
  hideUndetected: true,
  hiddenKinds: [],
  bbox: null,
  selectedId: null,
  commandLog: [],
  recent: [],
  camera: null,
  track: null,
  picker: null,
  highlightIds: [],
  world: null,
  resetNotice: null,
  auditOpen: false,

  // A fix from the server is authoritative, so it snaps the drawn positions to it rather
  // than easing toward them: the estimate existed only until the truth arrived.
  // ⚠️ ARRIVING IS NOT BEING DRAWN, so this no longer clears `loading`. The map does that
  // once it has actually painted the assets; see the asset effect in GlobeMap.
  setAssets: (a) => set({ assets: a, displayAssets: a, error: null }),
  setDisplayAssets: (a) => set({ displayAssets: a }),
  setMesh: (m) => set({ mesh: m }),
  setIce: (i) => set({ ice: i }),
  setIceDate: (d) => set({ iceDate: d }),
  // 🔑 STARTS AT `entry`, so the very first paint is the entry screen and the database is
  // never asked for anything before somebody has said they want it.
  phase: "entry",
  setPhase: (p) => set({ phase: p }),
  waiting: false,
  setWaiting: (v) => set({ waiting: v }),
  setLoading: (v) => set({ loading: v }),
  setError: (e) => set({ error: e, loading: false }),
  setPlacing: (p) => set({ placing: p }),
  setProjection: (p) => set({ projection: p }),
  setShowIce: (v) => set({ showIce: v }),
  setHideUndetected: (v) => set({ hideUndetected: v }),
  setHiddenKinds: (k) => set({ hiddenKinds: k }),
  toggleKind: (k) =>
    set((s) => ({
      hiddenKinds: s.hiddenKinds.includes(k)
        ? s.hiddenKinds.filter((x) => x !== k)
        : [...s.hiddenKinds, k],
    })),
  setWorld: (w) => set({ world: w }),
  setResetNotice: (n) => set({ resetNotice: n }),
  setAuditOpen: (v) => set({ auditOpen: v }),
  setBbox: (b) => set({ bbox: b }),
  select: (id) => set({ selectedId: id }),
  appendCommand: (e) => set((s) => ({ commandLog: [...s.commandLog, e].slice(-LOG_LIMIT) })),
  setCamera: (c) => set({ camera: c }),
  // Capped hard. This is the last thing said, not a session log.
  pushRecent: (t) =>
    set((s) => ({ recent: [...s.recent, t].slice(-RECENT_TURNS) })),
  setTrack: (t) => set({ track: t }),
  // Choosing one clears the list in the same update, so a stale pile can never sit over
  // the banner it just opened.
  setPicker: (p) => set({ picker: p }),
  setHighlight: (ids) => set({ highlightIds: ids }),
}));
