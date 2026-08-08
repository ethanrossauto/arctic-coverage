import { useEffect, useState } from "react";

/**
 * One clock for the whole display.
 *
 * 🔴 WHY THIS EXISTS RATHER THAN `Date.now()` INLINE. Three components called `Date.now()`
 * during render, which makes them impure: the value they compute changes every time React
 * happens to re-render, for reasons that have nothing to do with time passing. So the
 * "minutes since heard" figure in a banner would jump when you clicked something unrelated,
 * and would sit still when nothing else changed.
 *
 * For a console whose entire subject is FRESHNESS, that is a correctness problem rather
 * than a style one: the number an operator reads was a function of React's render schedule
 * instead of the clock.
 *
 * 🔑 IT IS THE SAME FIX THE BACKEND ALREADY MADE, at the other end of the wire. `overdue`
 * and `flag` are computed ONCE per request in `db.fetch_entities`, so the map, the status
 * strip and the mesh cannot disagree by a second across a threshold inside one response.
 * This is that idea for the client: one instant, read by everyone, advancing for exactly
 * one reason.
 *
 * ⚠️ THE DEFAULT INTERVAL IS 30 SECONDS, NOT ONE. Every threshold this display cares about
 * is measured in minutes or hours (see OVERDUE_MINUTES), so a per-second tick would
 * re-render the tree sixty times to change a number that moves once. Thirty seconds keeps
 * the worst case, a count crossing its threshold, visibly stale for well under a minute.
 *
 * Found by `react-hooks/purity` in eslint. It is not the sort of thing that shows up in
 * review, because the code reads perfectly sensibly.
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);

  return now;
}
