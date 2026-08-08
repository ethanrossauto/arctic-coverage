/**
 * The shared world's housekeeping: the idle clock, the reset, and what to say about both.
 *
 * 🔑 WHY THIS EXISTS AS A SURFACE AT ALL. There is one database and one world, so every
 * viewer is looking at the same thing and a reset lands on all of them. Giving each visitor
 * their own world would mean threading a session through every entity and every event row,
 * which is a different application. So the shared world is not hidden, it is disclosed: the
 * display says it is shared, says when it will reset, and says afterwards that it did.
 *
 * ⚠️ THE ALTERNATIVE WAS WORSE AND IT WAS THE BEHAVIOUR THAT SHIPPED. Before this, a viewer
 * reading the map for five minutes had it silently reset underneath them: placed assets
 * gone, audit log emptied, nothing on screen to say why. A world that changes without
 * explanation reads as a broken display, which is the one impression this build can least
 * afford.
 */

/** Where the idle clock stands, exactly as `/api/world` reports it. */
export interface WorldStatus {
  /** False when the reset is switched off, for a test run or a recording. */
  enabled: boolean;
  idleResetMinutes: number;
  idleSeconds?: number;
  /** Null when the reset is disabled. Zero means it is due on the next request. */
  secondsUntilReset?: number | null;
  /**
   * Changes exactly when the world is laid back down.
   *
   * 🔑 NOT A CLOCK, AND THAT IS THE POINT. A client that remembers the last value it saw
   * can tell the world changed underneath it without diffing anything, and it catches both
   * causes with one mechanism: the window elapsing, and somebody else pressing reset while
   * you are watching. The second has no countdown attached, because nothing can predict it.
   */
  generation?: string;
  cause?: string;
}

interface WireWorld {
  enabled: boolean;
  idle_reset_minutes: number;
  idle_seconds?: number;
  seconds_until_reset?: number | null;
  generation?: string;
  cause?: string;
  state?: string;
  error?: string;
}

function toWorld(w: WireWorld): WorldStatus {
  return {
    enabled: w.enabled,
    idleResetMinutes: w.idle_reset_minutes,
    idleSeconds: w.idle_seconds,
    secondsUntilReset: w.seconds_until_reset,
    generation: w.generation,
    cause: w.cause,
  };
}

/**
 * How close to the reset the countdown starts speaking, in seconds.
 *
 * 🔑 TWO MINUTES, SO AN ENGAGED VIEWER NEVER SEES IT TICK. The countdown only runs while
 * nobody is doing anything, and any deliberate act puts it back to the full window. Someone
 * working the display therefore never meets a clock in the corner of the screen, and neither
 * does the demo video. Somebody who has stopped gets two minutes of warning, which is enough
 * to do something about it and short enough that it is not nagging.
 */
export const COUNTDOWN_VISIBLE_S = 120;

/** Read the clock. Polling this is NOT activity: see `touchWorld`. */
export async function fetchWorld(): Promise<WorldStatus> {
  const res = await fetch("/api/world");
  if (!res.ok) throw new Error(`world request failed: ${res.status}`);
  return toWorld((await res.json()) as WireWorld);
}

/**
 * Tell the server a person deliberately did something.
 *
 * ⚠️ A DIFFERENT VERB FROM `fetchWorld` BECAUSE THEY MEAN OPPOSITE THINGS. Reading the
 * clock must not wind it, or one tab left open on a second monitor would hold the world
 * open forever and it would never reset for anybody. This is sent for acts a person
 * performs: panning, zooming, selecting, toggling a layer. Never for the poll.
 */
export async function touchWorld(): Promise<void> {
  await fetch("/api/world/touch", { method: "POST" }).catch(() => {});
}

/**
 * Did THIS tab cause the reset it is about to notice?
 *
 * ⚠️ WITHOUT THIS, PRESSING RESET TELLS YOU SOMEBODY ELSE DID IT. The notice is driven off
 * `generation` changing, which is what makes it catch the case nothing can predict, another
 * viewer resetting while you watch. The cost is that it cannot tell your own click apart
 * from theirs, and the honest-sounding sentence it produces is then simply false for the
 * one person who knows exactly what happened.
 *
 * Module state rather than store state on purpose: it exists for a few hundred milliseconds
 * between a click and the next poll, and nothing else should be able to read or reason
 * about it.
 */
let selfReset = false;

/** Ask for a reset. Returns the seconds to wait when the floor has not elapsed. */
export async function resetWorld(): Promise<{ ok: boolean; retryAfterS?: number }> {
  const res = await fetch("/api/reset", { method: "POST" });
  const body = (await res.json().catch(() => ({}))) as { retry_after_s?: number };
  if (res.status === 429) return { ok: false, retryAfterS: body.retry_after_s ?? 60 };
  if (res.ok) selfReset = true;
  return { ok: res.ok };
}

/** True once, for the tab that asked. Reading it clears it. */
export function consumeSelfReset(): boolean {
  const was = selfReset;
  selfReset = false;
  return was;
}

/** `95` becomes `1:35`. Seconds are what a countdown is for. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * What to tell a viewer whose world just changed.
 *
 * ⚠️ THE CAUSE IS READ, NEVER ASSUMED. An idle reset was announced by the countdown, so the
 * notice is a confirmation of something expected. A reset somebody else asked for arrived
 * with no warning at all, and calling that one a timeout would be telling the viewer
 * something untrue about their own session.
 */
export function resetNoticeFor(cause: string | undefined, minutes: number): string {
  if (cause === "manual") return "the shared world was reset by another viewer";
  return `the shared world was reset after ${Math.round(minutes)} minutes idle`;
}
