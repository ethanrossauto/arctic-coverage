/**
 * Shell: the map, a status strip, and the ice timebar.
 *
 * 🔑 EVERY VALUE READ HERE COMES THROUGH ITS OWN SELECTOR, never `useStore()` bare.
 * A selector-less read subscribes this component to the whole store, so appending one
 * line to the command transcript re-rendered the entire shell. With 68 assets and a
 * six-thousand-polygon ice layer downstream, that is not free.
 *
 * ⏱️ ONE TIME CONTROL ON SCREEN, and it is the ice timebar. See the note in store.ts
 * for why there is no scenario clock beside it.
 */
import { useEffect, useState } from "react";

import { fetchAssets, fetchIce, fetchMesh, isOverdue } from "./assets";
import { AssetBanner } from "./AssetBanner";
import { AssetPicker } from "./AssetPicker";
import { CommandBar } from "./CommandBar";
import { GlobeMap } from "./map/GlobeMap";
import { IceTimebar } from "./IceTimebar";
import { useStore } from "./store";
import { useNow } from "./useNow";

export default function App() {
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const ice = useStore((s) => s.ice);
  const iceDate = useStore((s) => s.iceDate);
  const loading = useStore((s) => s.loading);
  const error = useStore((s) => s.error);
  const projection = useStore((s) => s.projection);
  const bbox = useStore((s) => s.bbox);
  const showMesh = useStore((s) => s.showMesh);
  const setShowMesh = useStore((s) => s.setShowMesh);

  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const setIce = useStore((s) => s.setIce);
  const setError = useStore((s) => s.setError);
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

  // ⏱️ THE ONLY THING THAT MAKES MOTION VISIBLE. The server advances the position of
  // every asset it is currently hearing from, so the world changes between reads. Fetched
  // once at mount, the display would show a snapshot from whenever the tab was opened and
  // quietly never move again, which is indistinguishable from the feature not working.
  //
  // Five seconds is chosen against what it costs, not against how smooth it looks. Each
  // poll re-uploads 68 points and the link lines, and nothing else: the ice layer, which
  // is the expensive one at around 6,200 polygons, has its own effect keyed on its own
  // date and is untouched by this.
  //
  // ⚠️ Deliberately NOT an animation. Assets jump to the position the server reports
  // rather than being eased between samples, because interpolating would mean drawing
  // positions nobody measured, which is the same rule the ice layer follows.
  //
  // 🔒 `?live=off` FREEZES IT, and that exists for the tests rather than for operators.
  // Two of them compare one frame against another to prove a layer appeared or vanished,
  // and a world that moves underneath them turns a real assertion into a flaky one. The
  // honest fix is to stop the world for those tests and say so, not to loosen the
  // comparison until motion no longer trips it.
  const live = new URLSearchParams(location.search).get("live") !== "off";
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => {
      fetchAssets().then(setAssets).catch(() => {});
      fetchMesh().then(setMesh).catch(() => {});
    }, 5000);
    return () => clearInterval(id);
  }, [live, setAssets, setMesh]);

  // Ice rebuilds whenever the selected date changes. The whole measurement set is one
  // fetch, cached after the first call, so changing month costs no network at all.
  //
  // 🔴 DEFERRED ON FIRST LOAD, AND THAT IS NOT A MICRO-OPTIMISATION. The vendored
  // measurement set is 960 x 175, so one date is 168,000 cells and turning it into drawable
  // geometry is the most expensive thing this app does. Started at mount it wins the race
  // against the assets and holds the main thread, and the display sat with an empty map for
  // TWELVE SECONDS while the footer already said 76 assets. Measured, not guessed.
  //
  // The assets are what an operator is looking at and the ice is context behind them, so
  // the ice yields. Subsequent changes are immediate: by then there is nothing to race.
  const [iceReady, setIceReady] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setIceReady(true), 600);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!iceReady) return;
    fetchIce(iceDate)
      .then(setIce)
      .catch((e) => console.error("ice layer unavailable", e));
  }, [iceReady, iceDate, setIce]);

  // Counted here rather than in the store, because they are a function of the asset
  // list and nothing else. Caching a filter over 68 rows would cost more to invalidate
  // than to recompute.
  //
  // ⏱️ Read against wall time. The thresholds are hours (see OVERDUE_MINUTES), so a
  // count that refreshes when the assets do is as current as the data behind it.
  const now = useNow();
  const overdue = assets.filter((a) => isOverdue(a, now)).length;
  const dark = assets.filter((a) => a.aisReporting === false).length;

  return (
    <div className="app">
      <GlobeMap />

      <header className="strip">
        <span className="brand">ARCTIC COVERAGE</span>
        <span className="sep" />
        <button onClick={() => setProjection(projection === "globe" ? "mercator" : "globe")}>
          {projection === "globe" ? "GLOBE" : "MERCATOR"}
        </button>

        {/* A view setting, so it sits with the projection toggle rather than with the
            counts in the footer. The footer keeps reporting the mesh either way: hiding
            the lines hides the lines, it does not hide the fact. */}
        <label className="toggle">
          <input
            type="checkbox"
            checked={showMesh}
            onChange={(e) => setShowMesh(e.target.checked)}
          />
          MESH LINKS
        </label>
      </header>

      <AssetBanner />
      <AssetPicker />

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
