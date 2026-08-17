/**
 * When the console is live, when it steps aside, and who pays for the database waking up.
 *
 * 🔴 THE PROBLEM THIS SOLVES IS A BILL, AND THE BILL HAS A CLIFF. The database scales to
 * zero after five minutes with no queries, and waking it cost a measured 11.9 s on the first
 * connection, then 6.5, 1.4 and 0.7 as the compute came up. Keeping it awake around the
 * clock avoids that and spends roughly 6 compute-hours a day against a 100-hour monthly
 * allowance, and **exceeding the allowance suspends the compute outright for the rest of the
 * billing period.** So the naive fix and the naive failure are the same thing: a site that is
 * always instant until the day it is gone for a week.
 *
 * ⚠️ AND THE POLLING MEANT IT COULD NEVER SLEEP ANYWAY. Two timers, one here and one in the
 * shell, each queried the database every five seconds for as long as a tab was open. The
 * five-minute suspend window was therefore unreachable in practice: one forgotten tab kept
 * the compute up indefinitely, which is most of where those hours went.
 *
 * 🔑 SO THE DESIGN IS TO LET IT SLEEP, AND TO MAKE THE WAKING SOMEBODY ELSE'S IDEA. After
 * `IDLE_RETURN_MS` with no deliberate interaction the console steps back to an entry screen
 * and every poll stops. The database then suspends on its own, and the next visit pays the
 * wake behind a screen that is *supposed* to take a moment.
 *
 * 🔒 THE INVARIANT, AND IT IS THE WHOLE DESIGN:
 *
 *     THE DATABASE IS ONLY EVER ASLEEP WHILE THE VISITOR IS LOOKING AT THE ENTRY SCREEN.
 *
 * ⚠️ IT IS AN INVARIANT RATHER THAN A CHOICE OF NUMBERS, AND THAT DISTINCTION IS THE POINT.
 * The obvious version of this feature stops polling as soon as somebody goes idle and returns
 * them to the entry screen some minutes later. That opens a gap: polling stops, the database
 * suspends five minutes on, and the console goes on looking live until the return fires.
 * Anybody who comes back inside that gap gets the full cold-start stall with nothing
 * explaining it, which is worse than the behaviour being replaced. **So the poll does not
 * stop when a person goes idle. It stops when they are returned to the entry screen, as one
 * event, and the gap cannot exist.**
 *
 * The timeline, at the default fifteen minutes:
 *
 *     0:00   last deliberate interaction
 *     0:00+  polling continues, database stays awake, anything they do is fast
 *    15:00   returned to the entry screen AND polling stops, in the same moment
 *    20:00   database suspends, five minutes later, with nobody watching a live screen
 *     any    coming back starts on the entry screen, which wakes it before they can click
 *
 * ⛔ DO NOT "OPTIMISE" THIS BY STOPPING THE POLL EARLIER. It looks like free savings, it is
 * worth about five minutes of compute per session, and it reintroduces exactly the gap above.
 */

/**
 * Whether the console is being driven, or is waiting to be entered.
 *
 * ⚠️ `entry` IS NOT AN ERROR STATE AND NOT A DISCONNECTION. Nothing has failed and no data
 * has been lost; the shell stays mounted underneath so that coming back does not rebuild the
 * globe. It means only that nothing is being asked of the database.
 */
export type Phase = "entry" | "live";

/**
 * How long a person can leave the console alone before it steps back to the entry screen.
 *
 * 🔑 FIFTEEN MINUTES IS A DELIBERATE TRADE AND THE COST OF IT IS KNOWN. The tail on a session
 * is this number plus the database's own five-minute suspend window, so fifteen keeps the
 * compute up for twenty minutes after somebody stops touching it. Shorter saves compute and
 * interrupts people who were reading; longer costs compute and interrupts nobody.
 *
 * ⚠️ IT IS UNRELATED TO THE THIRTY-MINUTE WORLD RESET, which is a server-side rule about the
 * shared world going back to seed and is driven by deliberate interaction rather than by
 * polling. The two clocks answer different questions and are deliberately not the same
 * number: this one asks "is anyone here", that one asks "has the world drifted from seed".
 */
export const IDLE_RETURN_MS = 15 * 60_000;

/**
 * What a request has to exceed before the shell admits it is waiting.
 *
 * 🔑 A COLD DATABASE IS NOT THE ONLY WAY TO BE SLOW, so this is a floor on honesty rather
 * than a cold-start detector. The wake is normally paid behind the entry screen; this covers
 * the residual case where the compute suspends just as a request lands, and anything else
 * that surprises us later. Two seconds is comfortably above a warm read, which the poll
 * measures at about 0.65 s.
 */
export const SLOW_REQUEST_MS = 2_000;

/**
 * The endpoint that wakes the compute.
 *
 * 🔑 IT HAS TO RUN A REAL QUERY, which is why this is `healthz` and not a static file. An
 * asset is served from the edge and touches no database at all, so pinging one would warm
 * nothing. This is the same endpoint the keep-warm workflow uses, for the same reason.
 */
export const WAKE_URL = "/api/healthz";

/**
 * Start the database coming up, and do not wait for it.
 *
 * 🔑 CALLED WHEN THE ENTRY SCREEN APPEARS, NOT WHEN THE BUTTON IS PRESSED, and that is what
 * makes the screen worth having. The seconds a person spends reading the screen and deciding
 * to click are seconds the compute is already starting, so the gap between arriving and
 * asking for data is spent rather than wasted. Waking on the click would put the whole
 * eleven seconds in front of somebody who has just told us they are ready.
 *
 * ⚠️ THE EARLIEST WAKE IS NOT THIS ONE. `index.html` fires the same request from an inline
 * script, before this bundle has been fetched or parsed, which on a cold load is a head start
 * of seconds. This exists for the second and later entries in one page life, where that
 * script has long since run.
 *
 * 🔒 FAILURE IS IGNORED ON PURPOSE. Nobody is waiting on the result, and a wake that fails
 * changes nothing except that the entry that follows pays what it would have paid anyway. The
 * real request reports its own failure.
 */
export function wakeDatabase(): void {
  void fetch(WAKE_URL, { cache: "no-store" }).catch(() => {});
}
