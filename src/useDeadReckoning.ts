/**
 * Smooth motion between the five second fixes, without asking the server five times as often.
 *
 * 🔑 THE OBVIOUS FIX IS THE WRONG ONE. Assets move because the server advances them per
 * request, so the map appears to tick once per poll, and the tempting change is to poll
 * every second instead of every five. Measured, one `/api/entities` costs about 0.65 s
 * against this database, so a one second poll is a two thirds duty cycle that pile-ups turn
 * into a queue, and it multiplies database load by five for a picture nobody reads five
 * times a second.
 *
 * 🔑 SO THE POSITIONS ARE CARRIED FORWARD HERE INSTEAD. Two consecutive fixes give an
 * observed velocity per asset, and between fixes the marker is advanced along it. The next
 * fix replaces the estimate outright rather than correcting it, so error cannot accumulate:
 * the worst case is one tick of drift, thrown away five seconds later.
 *
 * ⚠️ IT DOES NOT REIMPLEMENT THE MOTION MODEL, AND THAT IS THE WHOLE POINT. The server's
 * routes, its drift headings and its turning away from land are all invisible here; this
 * only measures where each asset actually went and assumes it keeps going. A second copy of
 * the motion rules living in the browser is exactly the two-sources-of-truth problem this
 * codebase keeps deleting, and it would go stale the first time the server's model changed.
 *
 * ⚠️ THE VISIBLE COST, STATED RATHER THAN HIDDEN: an asset that turns holds its old bearing
 * until the next fix. At five seconds that is about 550 m for an aircraft at 400 km/h and
 * 28 m for a vessel, which is under a pixel at any zoom this is demonstrated at.
 */
import { useEffect, useRef } from "react";

import type { Asset } from "./assets";
import { useStore } from "./store";

/** How often the estimate is redrawn. */
const TICK_MS = 1000;

/**
 * How far ahead an estimate is trusted, in milliseconds.
 *
 * 🔒 A FIX THAT STOPPED ARRIVING MUST NOT BECOME A MARKER SAILING OFF FOREVER. If the poll
 * fails or the tab is backgrounded, this stops extrapolating rather than confidently drawing
 * a position nothing has confirmed for a minute.
 */
const MAX_EXTRAPOLATION_MS = 15_000;

/**
 * Above this implied speed, a jump is treated as a teleport rather than as travel.
 *
 * 🔴 A WORLD RESET MOVES EVERY ASSET AT ONCE, and reading that as velocity would fling the
 * whole map off screen at the exact moment it was supposed to look freshly seeded. Placing
 * or tasking an asset does the same thing on a smaller scale. Faster than any aircraft here
 * means it did not fly there.
 */
const MAX_IMPLIED_KMH = 1500;

const KM_PER_DEGREE = 111.32;

interface Fix {
  lat: number;
  lon: number;
}

export function useDeadReckoning(): void {
  const assets = useStore((s) => s.assets);
  const setDisplayAssets = useStore((s) => s.setDisplayAssets);

  // Velocity in degrees per millisecond, per asset id. Held in refs because it is this
  // tab's working estimate, not shared state anything else should reason about.
  const velocity = useRef(new Map<string, { dLat: number; dLon: number }>());
  const lastFix = useRef<{ at: number; byId: Map<string, Fix> }>({ at: 0, byId: new Map() });

  // ---- every server fix: measure what actually happened -----------------------------
  useEffect(() => {
    const now = Date.now();
    const previous = lastFix.current;
    const elapsed = now - previous.at;
    const byId = new Map<string, Fix>();
    const next = new Map<string, { dLat: number; dLon: number }>();

    for (const a of assets) {
      if (a.lat == null || a.lon == null) continue;
      byId.set(a.id, { lat: a.lat, lon: a.lon });

      const before = previous.byId.get(a.id);
      // No pair yet, or a gap so long the pair says nothing useful about now.
      if (!before || elapsed <= 0 || elapsed > MAX_EXTRAPOLATION_MS * 2) continue;

      const dLat = a.lat - before.lat;
      const dLon = a.lon - before.lon;
      // ⚠️ CROSSING THE ANTIMERIDIAN LOOKS LIKE A 360 DEGREE SPRINT. Every route here is
      // polar, so this is reachable, and guessing which way it went would be worse than
      // waiting one fix.
      if (Math.abs(dLon) > 180) continue;

      // Degrees to kilometres, with longitude narrowing toward the pole. Rough is fine:
      // this only has to separate travel from teleportation.
      const east = dLon * Math.cos((a.lat * Math.PI) / 180);
      const km = Math.hypot(dLat, east) * KM_PER_DEGREE;
      if (km / (elapsed / 3_600_000) > MAX_IMPLIED_KMH) continue;

      if (dLat === 0 && dLon === 0) continue; // stationary, so nothing to carry forward
      next.set(a.id, { dLat: dLat / elapsed, dLon: dLon / elapsed });
    }

    velocity.current = next;
    lastFix.current = { at: now, byId };
  }, [assets]);

  // ---- between fixes: carry the estimate forward ------------------------------------
  useEffect(() => {
    const id = setInterval(() => {
      const since = Date.now() - lastFix.current.at;
      if (since <= 0 || since > MAX_EXTRAPOLATION_MS) return;
      const v = velocity.current;
      if (v.size === 0) return;

      // 🔒 ALWAYS FROM THE LAST SERVER FIX, NEVER FROM THE LAST ESTIMATE. Reading the store's
      // own `assets` rather than the previous display array is what stops one tick's
      // rounding becoming the next tick's starting point.
      const base = useStore.getState().assets;
      const moved: Asset[] = base.map((a) => {
        const d = v.get(a.id);
        if (!d || a.lat == null || a.lon == null) return a;
        return { ...a, lat: a.lat + d.dLat * since, lon: a.lon + d.dLon * since };
      });
      setDisplayAssets(moved);
    }, TICK_MS);
    return () => clearInterval(id);
  }, [setDisplayAssets]);
}
