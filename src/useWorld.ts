/**
 * The shared world's clock, and the signal that somebody is actually here.
 *
 * 🔑 TWO JOBS THAT LOOK LIKE ONE AND ARE OPPOSITES. Reading the clock must not wind it, or
 * a tab left open on a second monitor would hold the world open forever and it would never
 * reset for anyone. Telling the server a person did something must wind it, or somebody
 * reading the map for half an hour gets it reset out from under them. Traffic cannot tell
 * those two apart, which is why the poll and the touch are different calls.
 */
import { useEffect } from "react";

import { fetchAssets, fetchMesh } from "./assets";
import { useStore } from "./store";
import { fetchWorld, isSelfReset, resetNoticeFor, touchWorld } from "./world";

/** How often the clock is read. Matched to the map's own poll: one rhythm, not two. */
const POLL_MS = 5000;

/**
 * The most often a deliberate act is reported, in milliseconds.
 *
 * 🔑 THROTTLED HARD BECAUSE PRECISION HERE IS WORTHLESS. The window is thirty minutes, so
 * a clock that is up to thirty seconds behind reality changes nothing anyone can perceive,
 * and the alternative is a write on every mouse movement.
 */
const TOUCH_THROTTLE_MS = 30_000;

export function useWorld(): void {
  const setWorld = useStore((s) => s.setWorld);
  const setResetNotice = useStore((s) => s.setResetNotice);
  const setAssets = useStore((s) => s.setAssets);
  const setMesh = useStore((s) => s.setMesh);
  const phase = useStore((s) => s.phase);

  // ---- read the clock, and notice when the world changed underneath us -------------
  useEffect(() => {
    // 🔒 SILENT ON THE ENTRY SCREEN. This tick is a real database query, and together with
    // the shell's asset poll it is why the compute could never reach its five-minute suspend
    // window: one open tab kept it billing for ever. Nothing here is urgent enough to keep a
    // database awake for somebody who has walked away. See `session.ts`.
    if (phase !== "live") return;

    let live = true;
    // Held in the closure rather than in the store: it is this tab's memory of what it last
    // saw, and putting it in shared state would invite something else to reason about it.
    let seen: string | undefined;

    const tick = async () => {
      try {
        const w = await fetchWorld();
        if (!live) return;
        setWorld(w);

        if (seen !== undefined && w.generation !== undefined && w.generation !== seen) {
          // 🔑 THE ONE MECHANISM COVERS BOTH CAUSES. An idle reset was announced by the
          // countdown, so this confirms something expected. Another viewer pressing the
          // button was announced by nothing at all, and from here the two are identical:
          // the world simply changed. Reading `cause` is what keeps the sentence honest.
          //
          // ⚠️ EXCEPT FOR THE TAB THAT PRESSED THE BUTTON, which knows perfectly well what
          // happened and would otherwise be told a stranger did it. Being driven off
          // `generation` is what lets this catch the unpredictable case, and it is also why
          // it cannot tell your own click from somebody else's without being told.
          if (!isSelfReset()) {
            setResetNotice(resetNoticeFor(w.cause, w.idleResetMinutes));
          }
          // Pull the fresh world now rather than waiting out the map's own poll. The reset
          // already happened server-side; this is only about how long the screen keeps
          // showing something that is no longer true.
          fetchAssets().then(setAssets).catch(() => {});
          fetchMesh().then(setMesh).catch(() => {});
        }
        seen = w.generation;
      } catch {
        // A clock that cannot be read is not worth taking the display down for. The next
        // tick tries again, and the disclosure line simply says nothing in the meantime.
      }
    };

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [phase, setWorld, setResetNotice, setAssets, setMesh]);

  // ---- report that a person is deliberately using the display ----------------------
  useEffect(() => {
    let last = 0;
    const onAct = () => {
      const now = Date.now();
      if (now - last < TOUCH_THROTTLE_MS) return;
      last = now;
      void touchWorld();
    };

    // ⚠️ THESE THREE, AND NOT `mousemove`. A cursor crossing the window on its way
    // somewhere else is not a person using this display, and counting it would make the
    // idle window unreachable on any machine with a lively desktop. Pointer down covers
    // clicking and dragging the globe, wheel covers zooming, and key down covers typing,
    // which between them are the things somebody does on purpose.
    //
    // Capture phase so a handler that stops propagation cannot silently switch this off.
    const opts = { capture: true, passive: true } as const;
    window.addEventListener("pointerdown", onAct, opts);
    window.addEventListener("wheel", onAct, opts);
    window.addEventListener("keydown", onAct, opts);
    return () => {
      window.removeEventListener("pointerdown", onAct, opts);
      window.removeEventListener("wheel", onAct, opts);
      window.removeEventListener("keydown", onAct, opts);
    };
  }, []);
}
