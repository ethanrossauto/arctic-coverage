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

import type { Asset } from "./assets";
import type { Site, Window } from "./api";
import type { PassInterval } from "./playback";
import type { ViewportBbox } from "./map/bounds";

export type Projection = "globe" | "mercator";

interface State {
  window: Window | null;
  /** The five asset kinds, straight from the database. Domain objects, never map shapes. */
  assets: Asset[];
  loading: boolean;
  error: string | null;

  /** Playback clock, ms since epoch. Not wall time: fast-forward moves it. */
  simClock: number;
  /** Multiplier on real time. 1 = live. Fast-forward jumps rather than speeds up. */
  running: boolean;

  projection: Projection;
  /** Last computed viewport box. What a command means by "the current window". */
  bbox: ViewportBbox | null;
  selectedId: string | null;

  setWindow: (w: Window) => void;
  setAssets: (a: Asset[]) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  setClock: (ms: number) => void;
  advance: (ms: number) => void;
  setRunning: (v: boolean) => void;
  setProjection: (p: Projection) => void;
  setBbox: (b: ViewportBbox) => void;
  select: (id: string | null) => void;
}

export const useStore = create<State>((set) => ({
  window: null,
  assets: [],
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

  setWindow: (w) => set({ window: w, loading: false, error: null, simClock: w.start }),
  setAssets: (a) => set({ assets: a }),
  setLoading: (v) => set({ loading: v }),
  setError: (e) => set({ error: e, loading: false }),
  setClock: (ms) => set({ simClock: ms }),
  advance: (ms) => set((s) => ({ simClock: s.simClock + ms })),
  setRunning: (v) => set({ running: v }),
  setProjection: (p) => set({ projection: p }),
  setBbox: (b) => set({ bbox: b }),
  select: (id) => set({ selectedId: id }),
}));

/** Sites indexed by id, for the many places that need one by reference. */
export function siteById(window: Window | null, id: string): Site | undefined {
  return window?.sites.find((s) => s.id === id);
}

/** Every link up at the current clock, as (site, satellite) id pairs. */
export function linksAt(window: Window | null, atMs: number): PassInterval[] {
  if (!window) return [];
  return window.passes.filter((p) => atMs >= p.aos && atMs <= p.los);
}
