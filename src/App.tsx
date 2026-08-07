/**
 * Shell: the map, plus a status strip.
 *
 * The panels, command bar and activity log land here later. What exists now is
 * everything Stage 0 needed to prove: real propagation from the deployed Python
 * function, drawn on a globe, advancing on a playback clock, with exact link
 * intervals and a working viewport bbox.
 *
 * The clock deserves a note. It advances from a requestAnimationFrame ticker
 * against real elapsed time, NOT by adding a fixed step per frame. A fixed step
 * makes playback speed depend on frame rate, so the same demo runs at different
 * speeds on different machines, which is the sort of thing you discover while
 * recording.
 */
import { useCallback, useEffect, useRef } from "react";

import { fetchWindow } from "./api";
import { fetchAssets, isOverdue } from "./assets";
import { GlobeMap } from "./map/GlobeMap";
import { nextEvent } from "./playback";
import { linksAt, useStore } from "./store";

const WINDOW_MINUTES = 90;

export default function App() {
  const { window: win, assets, loading, error, simClock, running, projection, bbox } = useStore();
  const setWindow = useStore((s) => s.setWindow);
  const setAssets = useStore((s) => s.setAssets);
  const setError = useStore((s) => s.setError);
  const setClock = useStore((s) => s.setClock);
  const advance = useStore((s) => s.advance);
  const setRunning = useStore((s) => s.setRunning);
  const setProjection = useStore((s) => s.setProjection);

  useEffect(() => {
    fetchWindow(new Date(), WINDOW_MINUTES)
      .then(setWindow)
      .catch((e) => setError(String(e)));
  }, [setWindow, setError]);

  // Assets load independently of the propagation window. Deliberately: a database
  // hiccup should not blank the map, and a slow propagation call should not delay
  // the assets, which are the thing an operator is actually looking at.
  useEffect(() => {
    fetchAssets()
      .then(setAssets)
      .catch((e) => setError(String(e)));
  }, [setAssets, setError]);

  // Advance the clock against real elapsed time.
  const last = useRef<number | null>(null);
  useEffect(() => {
    let raf = 0;
    const tick = (now: number) => {
      if (last.current !== null && running) advance(now - last.current);
      last.current = now;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [running, advance]);

  const active = linksAt(win, simClock);
  // Counted here rather than in the store: it depends on the clock, so caching it
  // would mean invalidating on every frame for a number that costs nothing.
  const overdue = assets.filter((a) => isOverdue(a, simClock)).length;
  const dark = assets.filter((a) => a.aisReporting === false).length;

  /**
   * Fast forward to the next AOS or LOS.
   *
   * The client already holds every event in the window, so this is a jump rather
   * than a query. Landing a few seconds BEFORE the event on purpose: cutting to
   * the exact instant hides the thing you wanted to see, which is the link coming
   * up.
   */
  const fastForward = useCallback(() => {
    if (!win) return;
    const next = nextEvent(win.passes, simClock);
    if (!next) return;
    setClock(next.at - 4000);
  }, [win, simClock, setClock]);

  const upcoming = win ? nextEvent(win.passes, simClock) : null;

  return (
    <div className="app">
      <GlobeMap />

      <header className="strip">
        <span className="brand">ARCTIC COVERAGE</span>
        <span className="sep" />
        <span className="clock">{new Date(simClock).toISOString().slice(0, 19).replace("T", "  ")}Z</span>
        <button onClick={() => setRunning(!running)}>{running ? "PAUSE" : "RUN"}</button>
        <button onClick={fastForward} disabled={!upcoming}>
          NEXT EVENT{upcoming ? ` (${upcoming.kind.toUpperCase()} in ${fmtGap(upcoming.at - simClock)})` : ""}
        </button>
        <button onClick={() => setProjection(projection === "globe" ? "mercator" : "globe")}>
          {projection === "globe" ? "GLOBE" : "MERCATOR"}
        </button>
      </header>

      <footer className="strip bottom">
        {loading && <span>propagating…</span>}
        {error && <span className="err">{error}</span>}
        {win && (
          <>
            <span>
              mask <b>{win.maskDeg}°</b>
            </span>
            <span>
              links up <b>{active.length}</b> / {win.passes.length} in window
            </span>
            <span>
              sats <b>{win.tracks.length}</b>
            </span>
            <span className="dim">
              {/* Proof the viewport contract works under globe projection: a
                  pole-centred camera legitimately reports every longitude. */}
              view {bbox ? (bbox.global ? "all longitudes" : `${bbox.west.toFixed(0)}…${bbox.east.toFixed(0)}°`) : "—"}
              {bbox?.wraps ? " (wraps)" : ""} · {bbox ? `${bbox.south.toFixed(0)}…${bbox.north.toFixed(0)}°` : ""}
            </span>
          </>
        )}

        {/* Outside the `win` guard on purpose. Assets and propagation load
            independently, so a propagation failure must not take the asset picture
            off the screen with it. These are the two numbers an operator actually
            watches, so they get their own slots rather than being folded into a
            total. */}
        {assets.length > 0 && (
          <>
            <span>
              assets <b>{assets.length}</b>
            </span>
            <span title="assets past the reporting threshold for their kind">
              overdue <b className={overdue ? "warn" : undefined}>{overdue}</b>
            </span>
            <span title="contacts held without an AIS broadcast">
              not broadcasting <b className={dark ? "alert" : undefined}>{dark}</b>
            </span>
          </>
        )}
      </footer>
    </div>
  );
}

function fmtGap(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}
