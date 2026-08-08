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
import { AuditPanel } from "./AuditPanel";
import { CommandBar } from "./CommandBar";
import { GlobeMap } from "./map/GlobeMap";
import { IceTimebar } from "./IceTimebar";
import { useStore } from "./store";
import { useNow } from "./useNow";
import { useWorld } from "./useWorld";
import { COUNTDOWN_VISIBLE_S, formatCountdown, resetWorld } from "./world";

export default function App() {
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const ice = useStore((s) => s.ice);
  const iceDate = useStore((s) => s.iceDate);
  const loading = useStore((s) => s.loading);
  const error = useStore((s) => s.error);
  const projection = useStore((s) => s.projection);
  const bbox = useStore((s) => s.bbox);
  const hideUndetected = useStore((s) => s.hideUndetected);
  const setHideUndetected = useStore((s) => s.setHideUndetected);
  const world = useStore((s) => s.world);
  const resetNotice = useStore((s) => s.resetNotice);
  const setResetNotice = useStore((s) => s.setResetNotice);
  const auditOpen = useStore((s) => s.auditOpen);
  const setAuditOpen = useStore((s) => s.setAuditOpen);

  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const setIce = useStore((s) => s.setIce);
  const setError = useStore((s) => s.setError);
  const setProjection = useStore((s) => s.setProjection);

  /** The confirmation, and the cooldown message when the floor has not elapsed. */
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  // The shared world's clock, and the signal that a person is actually here.
  useWorld();

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
  // 🔑 DETECTED UNKNOWN, WHICH IS THE ONLY UNKNOWN THIS STRIP MAY COUNT. We hold it and it
  // will not say what it is: a contact that genuinely arrived, identity missing.
  //
  // ⚠️ IT REPLACED A "not broadcasting" COUNT, AND THAT WAS A CORRECTION RATHER THAN A
  // RENAME. The old number was every contact with AIS off, which swept in the two buckets
  // whose detections never reached this console at all. So the strip reported contacts that
  // were deliberately absent from the map beside it, and the count and the picture could not
  // be reconciled by anyone reading both. A status strip may only count what it can show.
  const detectedUnknown = assets.filter((a) => a.detectedUnknown).length;

  // The countdown speaks only near the end. An engaged viewer never sees it, because any
  // deliberate act puts the clock back to the full window.
  const until = world?.enabled ? world.secondsUntilReset ?? null : null;
  const counting = until !== null && until <= COUNTDOWN_VISIBLE_S;

  const doReset = async () => {
    setResetBusy(true);
    setResetError(null);
    const r = await resetWorld();
    setResetBusy(false);
    if (r.ok) {
      setConfirmReset(false);
      return;
    }
    setResetError(`the world was reset moments ago; ${r.retryAfterS}s before it can go again`);
  };

  return (
    <div className={`app${auditOpen ? " auditopen" : ""}`}>
      <GlobeMap />

      <header className="strip">
        <span className="brand">ARCTIC COVERAGE</span>
        <span className="sep" />
        <button onClick={() => setProjection(projection === "globe" ? "mercator" : "globe")}>
          {projection === "globe" ? "GLOBE" : "MERCATOR"}
        </button>

        {/* ⛔ Deliberately not labelled with a count. One of these buckets is a contact
            nothing is holding, and putting a number for it in the top strip would be the
            console asserting knowledge it does not have.

            🔑 PHRASED AS HIDE, AND CHECKED BY DEFAULT. It was SHOW, unchecked, which drew
            the same map and said something weaker: an unticked box a viewer never touches
            leaves the display looking complete when it is not. A ticked HIDE admits on the
            face of the control that something is being kept back, which is the argument
            this display is actually making. */}
        <label
          className="toggle"
          title="contacts whose detection never reached this console: held by a sensor that cannot report, or held by nothing at all"
        >
          <input
            type="checkbox"
            checked={hideUndetected}
            onChange={(e) => setHideUndetected(e.target.checked)}
          />
          HIDE UNDETECTED UNKNOWN
        </label>
      </header>

      <AssetBanner />
      <AssetPicker />
      <AuditPanel />

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
            <span title="contacts we hold that are not saying what they are">
              detected unknown{" "}
              <b className={detectedUnknown ? "alert" : undefined}>{detectedUnknown}</b>
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

        {/* 🔑 THE DISCLOSURE IS ALWAYS ON SCREEN, and the countdown only speaks near the
            end. Everyone here is looking at one shared world, so a reset lands on all of
            them; that cannot be prevented without giving each visitor a world of their own,
            so it is said out loud instead. The number is named rather than described,
            because "resets when idle" leaves a viewer with no way to judge whether they
            have time to read something. */}
        {world?.enabled && (
          <span className={`worldline${counting ? " due" : ""}`}>
            shared demo world · resets{" "}
            {counting ? (
              <>
                in {formatCountdown(until as number)} ·{" "}
                <span className="act">interact to keep this session active</span>
              </>
            ) : (
              `after ${Math.round(world.idleResetMinutes)} min idle`
            )}
          </span>
        )}

        <span className="footbtns">
          {/* Labelled with the word and nothing else. A panel behind a toggle can still be
              missed, and the answer to that is a legible label rather than opening it
              uninvited over somebody's map. */}
          <button onClick={() => setAuditOpen(!auditOpen)} title="the server-side record of every command">
            AUDIT
          </button>
          <button onClick={() => { setResetError(null); setConfirmReset(true); }}>
            RESET WORLD
          </button>
        </span>
      </footer>

      {/* Announced rather than silent. A world that changes with no explanation reads as a
          broken display, which is the one impression this build can least afford. */}
      {resetNotice && (
        <div className="resetnotice" role="status">
          <span>{resetNotice}</span>
          <button onClick={() => setResetNotice(null)}>dismiss</button>
        </div>
      )}

      {confirmReset && (
        <div className="modal" role="dialog" aria-modal="true" aria-label="reset the world">
          <div className="modalcard">
            <h2>RESET WORLD TO SEED</h2>
            {/* ⚠️ IT SAYS "ALL USERS" BECAUSE IT MEANS IT. One database, one world. Someone
                who reads this as resetting only their own view would be wrong, and would
                find out by taking somebody else's session down with them. */}
            <p>
              <span className="shared">This resets the world for ALL USERS currently viewing,</span>{" "}
              not just you. Any placed assets and the command history will be cleared.
            </p>
            {resetError && <p className="err">{resetError}</p>}
            <div className="modalactions">
              <button onClick={() => setConfirmReset(false)}>CANCEL</button>
              <button className="danger" onClick={doReset} disabled={resetBusy}>
                {resetBusy ? "RESETTING…" : "RESET"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
