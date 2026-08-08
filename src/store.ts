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

import type { Asset, IceLayer, MeshStatus } from "./assets";
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
  /** Whether the timebar is stepping through the measurements on its own. */
  iceScrubbing: boolean;
  loading: boolean;
  error: string | null;

  projection: Projection;
  /**
   * Whether the radio link graph is drawn.
   *
   * 🔑 A VIEW SETTING, NOT A DOMAIN FACT. Hiding the lines does not stop the mesh being
   * computed, does not change what the footer counts, and does not change what a command
   * about connectivity answers. The mesh is one layer among several now rather than the
   * subject of the display, and on a dense cluster its lines are what you turn off to
   * read anything else.
   */
  showMesh: boolean;
  /**
   * Whether the sea-ice layer is drawn.
   *
   * "Show me the weather overlays" turns this on. 🔒 No server code reads the ice data and
   * none ever will: the command layer asks the display to show a layer it already has,
   * which is why this is client state and not a server fact.
   */
  showIce: boolean;
  /**
   * Whether contacts the network cannot confirm are drawn.
   *
   * 🔒 DEFAULT OFF, AND THAT IS THE POINT RATHER THAN A PREFERENCE. The default picture
   * shows what actually reached us. Revealing what it cannot confirm has to be a deliberate
   * act, because one of those buckets is a contact nothing is holding, which the console
   * has no honest basis for knowing about at all.
   */
  showUndetected: boolean;
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

  setAssets: (a: Asset[]) => void;
  setMesh: (m: MeshStatus) => void;
  setIce: (i: IceLayer) => void;
  setIceDate: (d: string) => void;
  setIceScrubbing: (v: boolean) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  setProjection: (p: Projection) => void;
  setShowMesh: (v: boolean) => void;
  setShowIce: (v: boolean) => void;
  setShowUndetected: (v: boolean) => void;
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
  mesh: null,
  ice: null,
  iceDate: new Date().toISOString().slice(0, 10),
  iceScrubbing: false,
  loading: true,
  error: null,
  // Projection can be pinned from the URL (?proj=mercator). Useful for linking
  // someone to a specific view, and it is how the renderer swap gets exercised
  // without a click during automated screenshots.
  projection: (new URLSearchParams(location.search).get("proj") === "mercator"
    ? "mercator"
    : "globe") as Projection,
  showMesh: true,
  showIce: true,
  showUndetected: false,
  bbox: null,
  selectedId: null,
  commandLog: [],
  recent: [],
  camera: null,
  track: null,
  picker: null,
  highlightIds: [],

  setAssets: (a) => set({ assets: a, loading: false, error: null }),
  setMesh: (m) => set({ mesh: m }),
  setIce: (i) => set({ ice: i }),
  setIceDate: (d) => set({ iceDate: d }),
  setIceScrubbing: (v) => set({ iceScrubbing: v }),
  setLoading: (v) => set({ loading: v }),
  setError: (e) => set({ error: e, loading: false }),
  setProjection: (p) => set({ projection: p }),
  setShowMesh: (v) => set({ showMesh: v }),
  setShowIce: (v) => set({ showIce: v }),
  setShowUndetected: (v) => set({ showUndetected: v }),
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
