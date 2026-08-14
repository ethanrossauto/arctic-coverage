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

import { fetchAssets, fetchIce, fetchMesh, isOverdue, MESH_KINDS } from "./assets";
import { AssetBanner } from "./AssetBanner";
import { AssetPicker } from "./AssetPicker";
import { AuditPanel } from "./AuditPanel";
import { CommandBar } from "./CommandBar";
import { GlobeMap } from "./map/GlobeMap";
import { IceTimebar } from "./IceTimebar";
import { useStore } from "./store";
import { PlaceMenu } from "./PlaceMenu";
import { ViewMenu } from "./ViewMenu";
import { useNow } from "./useNow";
import { useDeadReckoning } from "./useDeadReckoning";
import { useWorld } from "./useWorld";
import { COUNTDOWN_VISIBLE_S, formatCountdown, resetWorld } from "./world";

/**
 * One sentence saying what this is, for a visitor who followed a link here.
 *
 * ⚠️ IT HAS TO SURVIVE BEING READ IN A TOP BAR, which rules out the phrasing that already
 * exists in the page's meta description. "A natural-language interface to a deployable
 * sensor picture" is accurate and is written for a search result, not for a person glancing
 * at a header: it is three noun phrases deep before it says anything you can picture. The
 * two say the same thing and neither is the other's summary.
 */
const ABOUT = "A portfolio demo: an Arctic sensor network you can drive by typing or talking to it.";

/**
 * The same sentence for a screen that cannot hold the long one.
 *
 * ⚠️ IT IS NOT AN ABBREVIATION, IT IS THE SAME CLAIM SHORTER. Cutting the long line at a
 * comma would leave "an Arctic sensor network", which describes the map and drops the only
 * part a visitor cannot work out by looking: that the thing takes commands. So the short
 * version keeps the verb and gives up the framing instead.
 */
const ABOUT_SHORT = "Arctic sensor network demo, driven by typed or spoken commands.";

export default function App() {
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const iceDate = useStore((s) => s.iceDate);
  const loading = useStore((s) => s.loading);
  const error = useStore((s) => s.error);
  const projection = useStore((s) => s.projection);
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
  const [viewOpen, setViewOpen] = useState(false);
  const [placeOpen, setPlaceOpen] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  // The shared world's clock, and the signal that a person is actually here.
  useWorld();
  // Carries positions forward between the five second fixes. Costs no extra requests; see
  // the module for why polling faster was the wrong answer.
  useDeadReckoning();

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

  // 🔒 THE FAILURE PATH FOR THE LOADING CURTAIN. It is lifted when the asset icons actually
  // reach the screen, which is the right signal and is unreachable when the fetch failed:
  // no assets, no icons, no lift, and the viewer would sit behind "Loading..." until the
  // 15 second watchdog in index.html gave up. An error is a perfectly good reason to stop
  // waiting, and the strip below is already showing it, so let them see it.
  useEffect(() => {
    if (error) (window as { consoleReady?: () => void }).consoleReady?.();
  }, [error]);

  // Counted here rather than in the store, because they are a function of the asset
  // list and nothing else. Caching a filter over 68 rows would cost more to invalidate
  // than to recompute.
  //
  // ⏱️ Read against wall time. The thresholds are hours (see OVERDUE_MINUTES), so a
  // count that refreshes when the assets do is as current as the data behind it.
  const now = useNow();

  // 🔑 EVERY COUNT BELOW IS TAKEN OVER OUR OWN NETWORKED KIT, AND THAT IS WHAT MAKES THEM
  // ADD UP. Each condition tracked here is a fact about equipment we operate: whether we
  // can reach it, and what its last message said.
  //
  // ⛔ TWO THINGS ARE DELIBERATELY OUT OF THIS SET, for the same reason. A CONTACT is what
  // the network is watching rather than part of it. A RADAR SITE is friendly but not ours:
  // it carries `owned: false`, answers to its own operator and has never sent this console
  // anything, so it can be neither reachable nor overdue TO US. Counting either one left a
  // remainder no label could explain, and forced a third bucket into both groups whose only
  // member was the radar layer.
  const ours = assets.filter((a) => MESH_KINDS.has(a.kind));

  // ── current status: can what this asset knows get here, right now ──────────────
  //
  // ⚠️ TAKEN FROM THE SERVER, NOT RECOMPUTED. Reachability is a property of the whole link
  // graph, and this component holds no gateway roles, no ranges and no view of which relays
  // are being heard. Counting it here would be a second answer to a question the server has
  // already answered exactly.
  //
  // 🔑 BUILT BY SUBTRACTION SO THE PAIR CANNOT FAIL TO SUM. The cut-off bucket is the union
  // of "no route home" and "past its own threshold", and `reachable` is whatever is left.
  // Counting both independently would let an asset land in neither, and the strip would
  // then contradict the total printed beside it.
  const cutOffIds = mesh ? new Set(mesh.unreachable) : null;
  // ⚠️ NULL UNTIL THE MESH LOADS, AND RENDERED AS "…" RATHER THAN AS 0. A zero would read
  // as "nothing is cut off", which is the one answer that must never come from not having
  // looked. Same rule the server's own scans follow.
  const unreachable = cutOffIds
    ? ours.filter((a) => cutOffIds.has(a.id) || isOverdue(a, now)).length
    : null;
  const reachable = unreachable === null ? null : ours.length - unreachable;

  // ── last message: what this asset last told us about itself ────────────────────
  //
  // 🔑 A DIFFERENT QUESTION FROM THE ONE ABOVE, WHICH IS WHY BOTH ARE ON SCREEN. An asset
  // cut off an hour ago still has a last message, and it said the kit was fine. So the two
  // readings differ, and the gap between them is the story: everything last reported
  // healthy, and we can only currently reach some of it.
  //
  // ⚠️ NOMINAL IS THE COMPLEMENT, NOT ITS OWN FILTER, so this pair sums like the one above.
  // The cost, stated rather than hidden: an asset that has somehow never sent a message
  // reads as nominal here, because with the radar layer out of the set there is no third
  // bucket left for it to fall into. Nothing seeded or placeable is in that state today.
  const maintenance = ours.filter((a) => a.flag === "maintenance").length;
  const nominal = ours.length - maintenance;
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

        <ViewMenu open={viewOpen} onOpenChange={setViewOpen} />

        {/* Beside VIEW because they are the same sort of control: a menu that changes what
            the map is doing. VIEW decides what is drawn, PLACE puts something new there. */}
        <PlaceMenu open={placeOpen} onOpenChange={setPlaceOpen} />

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

        {/* 🔑 FOR SOMEONE WHO ARRIVED FROM A LINK AND HAS NO IDEA WHAT THIS IS. Everything
            else on screen assumes you already know: the strip counts assets, the command bar
            waits for a command, and the globe is a globe. A visitor who followed "my other
            portfolio project" from somewhere else lands on all of that with no sentence
            telling them what they are looking at or that they can type into it.

            ⚠️ IT SAYS "DEMO" ON PURPOSE. A console that looks operational and is not sets an
            expectation it then fails, and an employer who works out for themselves that this
            is a build rather than a product has learned it in the least flattering order. */}
        {/* 🔑 TWO LENGTHS, BOTH IN THE MARKUP, SWAPPED BY CSS. The visitor this line exists
            for is as likely to arrive on a phone as on a desktop, and the desktop sentence
            does not fit beside the controls much below a laptop. Rendering both and letting
            the stylesheet choose keeps the decision next to the widths that force it, in the
            one file that can measure them. */}
        <span className="about long">{ABOUT}</span>
        <span className="about short">{ABOUT_SHORT}</span>
      </header>

      <AssetBanner />
      <AssetPicker />
      <AuditPanel />

      <CommandBar />

      {/* 🔑 THE DISCLOSURE AND THE WORLD CONTROLS RIDE ON THE ICE ROW, not in the strip
          below it, so that strip holds counts and nothing else. A status strip that also
          carries a paragraph of disclosure and two buttons is a strip whose numbers have to
          be hunted for, and the numbers are the reason it exists. */}
      <IceTimebar>
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
            LLM AUDIT
          </button>
          <button onClick={() => { setResetError(null); setConfirmReset(true); }}>
            RESET WORLD
          </button>
        </span>
      </IceTimebar>

      <footer className="strip bottom">
        {loading && <span>loading…</span>}
        {error && <span className="err">{error}</span>}

        {assets.length > 0 && (
          <>
            <span
              className="grp"
              title="the equipment we operate and can hear from. Excludes contacts, which are what this network watches rather than part of it, and the early-warning radar sites, which are friendly but not ours and report to their own operator"
            >
              our assets <b>{ours.length}</b>
            </span>

            {/* 🔑 BOTH GROUPS SUM TO THE FRIENDLY TOTAL, and that is the property to protect
                when editing either of them. Two independent readings of the same set: one
                says whether we can hear it, the other says what it last told us. A number
                on this strip that belongs to neither reading is a number nobody can
                reconcile with the total printed beside it. */}
            <span className="grp">
              <span className="glabel">current status</span>
              <span title="something we are hearing from leads all the way back to a backhaul">
                reachable <b>{reachable ?? "…"}</b>
              </span>
              <span className="sep" />
              <span title="no live route home, or past its own reporting threshold. Either way nothing it knows is arriving here">
                unreachable/overdue{" "}
                <b className={unreachable ? "warn" : undefined}>{unreachable ?? "…"}</b>
              </span>
            </span>

            <span className="grp">
              <span className="glabel">last message</span>
              <span title="its last report said the kit was fine">
                nominal <b>{nominal}</b>
              </span>
              <span className="sep" />
              <span title="its last report said the kit needs attention">
                maintenance <b className={maintenance ? "warn" : undefined}>{maintenance}</b>
              </span>
            </span>

            {/* 🔑 RIGHT ALIGNED AND BOLD BECAUSE IT IS NOT ONE OF THE TOTALS. Everything to
                the left counts our own kit twice over, two questions about one set. This
                counts contacts, so it deliberately sits apart rather than reading as a
                third group somebody would try to add up with the others. */}
            <span
              className="unkcount"
              title="contacts we hold that will not say what they are. Counted apart from the totals: a contact is not one of ours"
            >
              detected unknown{" "}
              <b className={detectedUnknown ? "alert" : undefined}>{detectedUnknown}</b>
            </span>
          </>
        )}

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
