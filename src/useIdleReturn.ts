/**
 * Returns the console to the entry screen once nobody has touched it for a while.
 *
 * 🔒 THIS IS THE EVENT THAT ALSO STOPS THE POLLING, and they are one event on purpose. See
 * `session.ts` for the invariant and for why splitting them reintroduces a window where the
 * database is asleep behind a screen that still looks live.
 *
 * ⚠️ THE ACTIVITY SIGNAL IS THE SAME THREE EVENTS THE WORLD CLOCK USES, and it is copied
 * rather than shared because the two answer different questions. `useWorld` reports
 * deliberate use to the SERVER, throttled to one call every thirty seconds, so the shared
 * world knows somebody is here. This needs the local timestamp on every event, unthrottled,
 * and must not make a request to get it. Sharing one handler would mean either sending thirty
 * times more requests or reading an idle clock that updates twice a minute.
 */
import { useEffect, useRef } from "react";

import { IDLE_RETURN_MS } from "./session";
import { useStore } from "./store";

/**
 * How often the clock is examined.
 *
 * 🔑 POLLING A TIMESTAMP RATHER THAN SETTING A TIMER PER EVENT. A `setTimeout` reset on every
 * pointer event is a timer torn down and rebuilt on every frame of a drag, and it drifts
 * whenever the tab is throttled in the background. Reading a number every ten seconds costs
 * nothing and cannot drift, and ten seconds of imprecision against fifteen minutes is not a
 * quantity anybody can perceive.
 */
const CHECK_MS = 10_000;

export function useIdleReturn(): void {
  const phase = useStore((s) => s.phase);
  const setPhase = useStore((s) => s.setPhase);

  // Not state: it changes on every pointer event and nothing renders from it.
  //
  // ⚠️ SEEDED WITH ZERO RATHER THAN THE CLOCK, because reading the clock while rendering is
  // impure and the linter is right to refuse it. Nothing observes the zero: the effect below
  // sets a real timestamp the moment the console goes live, which it has to do anyway so that
  // somebody returning after twenty minutes away is not bounced straight back out.
  const lastActivity = useRef(0);

  useEffect(() => {
    const mark = () => {
      lastActivity.current = Date.now();
    };

    // ⚠️ THE SAME THREE AS `useWorld`, AND NOT `mousemove`. A cursor crossing the window on
    // its way somewhere else is not somebody using this display, and counting it would make
    // the idle window unreachable on any machine with a lively desktop.
    //
    // Capture phase so a handler that stops propagation cannot silently switch this off.
    const opts = { capture: true, passive: true } as const;
    window.addEventListener("pointerdown", mark, opts);
    window.addEventListener("wheel", mark, opts);
    window.addEventListener("keydown", mark, opts);
    return () => {
      window.removeEventListener("pointerdown", mark, opts);
      window.removeEventListener("wheel", mark, opts);
      window.removeEventListener("keydown", mark, opts);
    };
  }, []);

  useEffect(() => {
    // Nothing to time out of: the entry screen is already where an idle visitor belongs, and
    // leaving a timer running there would fight the click that sends them the other way.
    if (phase !== "live") return;

    // 🔑 RE-ARMED ON ENTRY. Somebody who returns after twenty minutes away has a stale
    // timestamp, and without this they would be sent straight back out by the first check.
    lastActivity.current = Date.now();

    const id = setInterval(() => {
      if (Date.now() - lastActivity.current < IDLE_RETURN_MS) return;
      setPhase("entry");
    }, CHECK_MS);
    return () => clearInterval(id);
  }, [phase, setPhase]);
}
