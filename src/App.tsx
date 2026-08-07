/**
 * Shell: the map, a status strip, and the ice timebar.
 *
 * The clock deserves a note. It advances from a requestAnimationFrame ticker
 * against real elapsed time, NOT by adding a fixed step per frame. A fixed step
 * makes playback speed depend on frame rate, so the same demo runs at different
 * speeds on different machines, which is the sort of thing you discover while
 * recording.
 *
 * 🔑 TWO TIME CONTROLS, AND THEY ARE NOT THE SAME CONTROL. The clock moves through
 * minutes of one scenario and animates things that move: vessels along their tracks,
 * a drone in transit. The timebar moves through five years of monthly satellite
 * measurements. Merging them sounds tidy and would mean nudging a vessel forward also
 * jumped the ice by a month.
 */
import { useEffect, useRef } from "react";

import { fetchAssets, fetchIce, fetchMesh, isOverdue } from "./assets";
import { CommandBar } from "./CommandBar";
import { GlobeMap } from "./map/GlobeMap";
import { IceTimebar } from "./IceTimebar";
import { useStore } from "./store";

export default function App() {
  const { assets, mesh, ice, iceDate, loading, error, simClock, running, projection, bbox } = useStore();
  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const setIce = useStore((s) => s.setIce);
  const setError = useStore((s) => s.setError);
  const advance = useStore((s) => s.advance);
  const setRunning = useStore((s) => s.setRunning);
  const setProjection = useStore((s) => s.setProjection);

  // Assets are the thing an operator is actually looking at, so they load on their
  // own and a failure in any derived layer below must not blank them.
  useEffect(() => {
    fetchAssets()
      .then(setAssets)
      .catch((e) => setError(String(e)));
  }, [setAssets, setError]);

  // The link graph loads independently, and a failure here is deliberately NOT fatal:
  // you can still see where everything is without knowing what can talk to what.
  useEffect(() => {
    fetchMesh()
      .then(setMesh)
      .catch((e) => console.error("mesh graph unavailable", e));
  }, [setMesh]);

  // Ice rebuilds whenever the selected date changes. The whole measurement set is one
  // fetch, cached after the first call, so dragging the timebar costs no network at all.
  useEffect(() => {
    fetchIce(iceDate)
      .then(setIce)
      .catch((e) => console.error("ice layer unavailable", e));
  }, [iceDate, setIce]);

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

  // Counted here rather than in the store: these depend on the clock, so caching them
  // would mean invalidating on every frame for a number that costs nothing.
  const overdue = assets.filter((a) => isOverdue(a, simClock)).length;
  const dark = assets.filter((a) => a.aisReporting === false).length;

  return (
    <div className="app">
      <GlobeMap />

      <header className="strip">
        <span className="brand">ARCTIC COVERAGE</span>
        <span className="sep" />
        <span className="clock">{new Date(simClock).toISOString().slice(0, 19).replace("T", "  ")}Z</span>
        <button onClick={() => setRunning(!running)}>{running ? "PAUSE" : "RUN"}</button>
        <button onClick={() => setProjection(projection === "globe" ? "mercator" : "globe")}>
          {projection === "globe" ? "GLOBE" : "MERCATOR"}
        </button>
      </header>

      <CommandBar />

      <IceTimebar />

      <footer className="strip bottom">
        {loading && <span>loading…</span>}
        {error && <span className="err">{error}</span>}

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
            {mesh && (
              <span title="radio links up, connected groups, and assets on no mesh at all">
                mesh <b>{mesh.links.length}</b> links · <b>{mesh.groups.length}</b> groups ·{" "}
                <b className={mesh.isolated.length ? "warn" : undefined}>{mesh.isolated.length}</b> isolated
              </span>
            )}
          </>
        )}

        {ice && (
          <span className="dim" title={`${ice.caveat}\n\n${ice.citation}`}>
            ice <b>{(ice.extentKm2 / 1e6).toFixed(1)}M km²</b> extent
          </span>
        )}

        <span className="dim">
          {/* Proof the viewport contract works under globe projection: a
              pole-centred camera legitimately reports every longitude. */}
          view {bbox ? (bbox.global ? "all longitudes" : `${bbox.west.toFixed(0)}…${bbox.east.toFixed(0)}°`) : "—"}
          {bbox?.wraps ? " (wraps)" : ""} · {bbox ? `${bbox.south.toFixed(0)}…${bbox.north.toFixed(0)}°` : ""}
        </span>
      </footer>
    </div>
  );
}
