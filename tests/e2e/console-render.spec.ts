import { expect, test } from "@playwright/test";

import { mapColourCount, readClock, waitForWindowLoaded } from "./helpers";

/**
 * Does the console actually work? Six questions, none of which any other test in
 * this repo can answer.
 *
 * Every assertion here is about BEHAVIOUR, never appearance. There is no golden
 * image and there never should be: a screenshot comparison on a WebGL globe fails
 * on driver differences, needs regenerating after every visual tweak, and reports
 * "something changed" when the useful question is "is it broken". What it looks
 * like is a human's call.
 */

test("the map paints real geometry, not a flat background", async ({ page }) => {
  /**
   * The blank-canvas guard. A style that fails to load, a source that will not
   * parse, or a WebGL context that never initialises all leave a canvas holding
   * one flat colour, and the surrounding UI keeps working perfectly, which is what
   * makes it so easy to miss.
   *
   * Land, the graticule and the ocean are three distinct colours before anything
   * dynamic is drawn, so a healthy first paint is comfortably above the threshold
   * and a blank one is at 1.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);
  await expect
    .poll(() => mapColourCount(page), { timeout: 30_000, intervals: [500] })
    .toBeGreaterThan(3);
});

test("server data reaches the UI", async ({ page }) => {
  await page.goto("/");
  await waitForWindowLoaded(page);

  const footer = page.locator(".strip.bottom");
  await expect(footer).toContainText("mask");
  await expect(footer).toContainText("15");
  await expect(footer).toContainText("sats 3");
  await expect(footer).toContainText("sites 5");
  // The window must contain passes, or the demo has nothing to show. This is the
  // UI-side echo of the Python scenario test that guards the constellation's
  // inclination.
  await expect(footer).toContainText(/\d+ in window/);
  const text = (await footer.innerText()).match(/(\d+) in window/);
  expect(Number(text?.[1] ?? 0), "pass intervals in the window").toBeGreaterThan(0);
});

test("the playback clock advances", async ({ page }) => {
  await page.goto("/");
  await waitForWindowLoaded(page);

  const first = await readClock(page);
  await page.waitForTimeout(2500);
  const second = await readClock(page);

  expect(
    second.getTime() - first.getTime(),
    "clock should advance roughly with real time",
  ).toBeGreaterThan(1000);
});

test("fast forward jumps the clock to the next event", async ({ page }) => {
  /**
   * The demo-insurance feature, and the one most likely to break silently. It
   * works because the client already holds every pass interval in the window, so
   * the jump is a scan of an array rather than a request. If that ever regresses
   * into a round trip, or the event list arrives empty, this test fails while the
   * page still looks fine.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);

  await page.getByRole("button", { name: /PAUSE/ }).click(); // freeze real advance
  const before = await readClock(page);

  const nextEvent = page.getByRole("button", { name: /NEXT EVENT/ });
  await expect(nextEvent).toBeEnabled();
  await nextEvent.click();

  const after = await readClock(page);
  const jumpS = (after.getTime() - before.getTime()) / 1000;
  // A jump, not a tick: the clock is paused, so any movement at all is the
  // feature working, and a real gap to the next AOS or LOS is seconds to minutes.
  expect(jumpS, "clock should jump forward").toBeGreaterThan(1);
});

test("the projection toggle switches between globe and mercator without erroring", async ({
  page,
}) => {
  /**
   * The globe is the default and mercator is the fallback, and both have to work,
   * because the fallback is what makes the globe safe to have promoted into the
   * base scope. `setProjection` is also the one MapLibre call in this app that
   * silently does nothing if made at the wrong moment.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);

  const toggle = page.getByRole("button", { name: /GLOBE|MERCATOR/ });
  await expect(toggle).toHaveText("GLOBE");
  await toggle.click();
  await expect(toggle).toHaveText("MERCATOR");

  // Still painting after the switch, which is the part that could break.
  await expect
    .poll(() => mapColourCount(page), { timeout: 20_000, intervals: [500] })
    .toBeGreaterThan(3);

  await toggle.click();
  await expect(toggle).toHaveText("GLOBE");
});

test("a pole-centred globe view reports every longitude", async ({ page }) => {
  /**
   * The viewport contract, asserted end to end.
   *
   * "Show me assets in the current zoom window" is one of the brief's own example
   * commands, and on a globe looking down at the pole the honest answer for
   * longitude is "all of them". MapLibre returns exactly that, and the command
   * layer has to carry it rather than treat it as a bug. This is the app's DEFAULT
   * view, so if this ever silently became a bounded range, every polar filter
   * would start dropping assets.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);
  await expect(page.locator(".strip.bottom")).toContainText("view all longitudes");
});
