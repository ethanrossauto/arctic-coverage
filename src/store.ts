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
 * Time lives here too. `simClock` is the playback position, which is separate
 * from wall-clock time because fast-forward exists. Everything that renders reads
 * `simClock`; nothing reads `Date.now()` except the ticker that advances it.
 */
import { create } from "zustand";

import type { Asset, IceLayer, MeshStatus } from "./assets";
import type { ViewportBbox } from "./map/bounds";

export type Projection = "globe" | "mercator";

/** One line in the on-screen transcript. */
export interface CommandEntry {
  role: "user" | "system";
  text: string;
  /** Which tier answered: the deterministic parser, or the model. Systems lines only. */
  tier?: string;
  ok?: boolean;
  source?: "typed" | "voice";
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
   * 🔑 Separate from `simClock`, and the two answer different questions. The slider moves
   * across FIVE YEARS of monthly satellite measurements; the clock moves across MINUTES of
   * a single scenario, animating vessels and drone transits. Folding them into one control
   * would mean nudging a vessel along its track also jumped the ice by a month.
   *
   * It only ever holds one of the vendored measurement dates. Nothing interpolates between
   * them, because a value between two measurements is a value nobody observed.
   */
  iceDate: string;
  /** Whether the timebar is stepping through the measurements on its own. */
  iceScrubbing: boolean;
  loading: boolean;
  error: string | null;

  /** Playback clock, ms since epoch. Not wall time: fast-forward moves it. */
  simClock: number;
  /** Whether the clock is advancing. Fast-forward jumps rather than speeding it up. */
  running: boolean;

  projection: Projection;
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
  /** Where a command asked the camera to go. Null until one does. */
  camera: CameraTarget | null;

  setAssets: (a: Asset[]) => void;
  setMesh: (m: MeshStatus) => void;
  setIce: (i: IceLayer) => void;
  setIceDate: (d: string) => void;
  setIceScrubbing: (v: boolean) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  setClock: (ms: number) => void;
  advance: (ms: number) => void;
  setRunning: (v: boolean) => void;
  setProjection: (p: Projection) => void;
  setBbox: (b: ViewportBbox) => void;
  select: (id: string | null) => void;
  appendCommand: (e: CommandEntry) => void;
  setCamera: (c: CameraTarget | null) => void;
}

/** How many transcript lines to keep. Enough to follow a demo, not a scrollback. */
const LOG_LIMIT = 40;

export const useStore = create<State>((set) => ({
  assets: [],
  mesh: null,
  ice: null,
  iceDate: new Date().toISOString().slice(0, 10),
  iceScrubbing: false,
  loading: true,
  error: null,
  simClock: Date.now(),
  running: true,
  // Projection can be pinned from the URL (?proj=mercator). Useful for linking
  // someone to a specific view, and it is how the renderer swap gets exercised
  // without a click during automated screenshots.
  projection: (new URLSearchParams(location.search).get("proj") === "mercator"
    ? "mercator"
    : "globe") as Projection,
  bbox: null,
  selectedId: null,
  commandLog: [],
  camera: null,

  setAssets: (a) => set({ assets: a, loading: false, error: null }),
  setMesh: (m) => set({ mesh: m }),
  setIce: (i) => set({ ice: i }),
  setIceDate: (d) => set({ iceDate: d }),
  setIceScrubbing: (v) => set({ iceScrubbing: v }),
  setLoading: (v) => set({ loading: v }),
  setError: (e) => set({ error: e, loading: false }),
  setClock: (ms) => set({ simClock: ms }),
  advance: (ms) => set((s) => ({ simClock: s.simClock + ms })),
  setRunning: (v) => set({ running: v }),
  setProjection: (p) => set({ projection: p }),
  setBbox: (b) => set({ bbox: b }),
  select: (id) => set({ selectedId: id }),
  appendCommand: (e) => set((s) => ({ commandLog: [...s.commandLog, e].slice(-LOG_LIMIT) })),
  setCamera: (c) => set({ camera: c }),
}));
