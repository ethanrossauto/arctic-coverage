import { expect, test } from "@playwright/test";

import { landPixelFraction, readClock, waitForWindowLoaded } from "./helpers";

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
   * The blank-map guard, and the test that would have caught a broken production
   * deploy. A style that fails to load, a source that will not parse, or a
   * geometry worker that 404s all leave the map empty while the surrounding UI
   * keeps working perfectly and nothing throws.
   *
   * Asserted on the LAND FILL COLOUR specifically, because that colour cannot
   * appear unless the source loaded, the worker parsed it and the GPU drew it.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);
  // A healthy polar view is roughly a quarter land. A broken one is zero.
  await expect
    .poll(() => landPixelFraction(page), { timeout: 30_000, intervals: [500] })
    .toBeGreaterThan(0.05);
});

test("server data reaches the UI", async ({ page }) => {
  await page.goto("/");
  await waitForWindowLoaded(page);

  const footer = page.locator(".strip.bottom");
  await expect(footer).toContainText("mask");
  await expect(footer).toContainText("15");
  await expect(footer).toContainText("sats 3");
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
    .poll(() => landPixelFraction(page), { timeout: 20_000, intervals: [500] })
    .toBeGreaterThan(0.05);

  await toggle.click();
  await expect(toggle).toHaveText("GLOBE");
});

test("a pole-centred globe view reports every longitude", async ({ page }) => {
  /**
   * The viewport contract, asserted end to end.
   *
   * "Show me assets in the current zoom window" is one of the supported example
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

test("all six asset kinds load and reach the map", async ({ page }) => {
  /**
   * The asset picture is the point of the application, so its arrival is asserted
   * end to end rather than inferred from the map not being blank.
   *
   * Counts are exact, not "greater than zero". A seed that silently drops a kind
   * would still satisfy a loose assertion, and a missing kind is invisible on a
   * globe covered in dots.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const counts: Record<string, number> = {};
  for (const a of body.entities) counts[a.kind] = (counts[a.kind] ?? 0) + 1;
  expect(counts).toEqual({ node: 24, patrol: 3, uas: 5, hydrophone: 10, vessel: 8, radar: 12 });

  await expect(page.locator(".strip.bottom")).toContainText("assets 62");
});

test("the two non-broadcasting contacts are surfaced and are the only ones", async ({ page }) => {
  /**
   * 🥇 The most important assertion about the data. A contact held without an AIS
   * broadcast is the case the whole system exists for, and the count is exactly two
   * by design: one anomaly reads as a fluke, five reads as a game. If a seed change
   * ever makes it zero, the demo loses its point silently.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const dark = body.entities.filter((a: { ais_reporting: boolean | null }) => a.ais_reporting === false);
  expect(dark.map((a: { name: string }) => a.name).sort()).toEqual(["UNKNOWN 01", "UNKNOWN 02"]);
  // Each is held by a named sensor, not by its own report. Provenance is what makes
  // an unidentified contact actionable rather than just alarming.
  for (const d of dark) {
    expect(d.props.first_detected_by, `${d.name} should name the sensor holding it`).toMatch(/^hyd-/);
  }
  await expect(page.locator(".strip.bottom")).toContainText("not broadcasting 2");
});

test("overdue assets are counted, and it is neither none nor all of them", async ({ page }) => {
  /**
   * "What has gone quiet" is only a real question if the answer is a subset. A seed
   * where nothing is overdue makes the feature undemonstrable; one where everything
   * is overdue makes it meaningless. This asserts the designed middle.
   */
  await page.goto("/");
  await waitForWindowLoaded(page);
  const footer = page.locator(".strip.bottom");
  await expect(footer).toContainText(/overdue \d+/);
  const text = await footer.innerText();
  const overdue = Number(text.match(/overdue (\d+)/)?.[1] ?? -1);
  expect(overdue).toBeGreaterThan(0);
  expect(overdue).toBeLessThan(50);
});

test("the radar layer is present, unowned, and never counted as overdue", async ({ page }) => {
  /**
   * The existing early-warning line is modelled as infrastructure to work alongside,
   * not as owned kit, and it reports into nothing. Two properties follow, and both
   * are easy to break silently:
   *
   *   - `owned: false`, so a count of "my assets" never quietly includes twelve sites
   *     belonging to somebody else.
   *   - no `last_heard`, so it cannot be overdue. Giving radar a reporting threshold
   *     would make all twelve permanently overdue and bury the four assets that
   *     genuinely are.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const radars = body.entities.filter((a: { kind: string }) => a.kind === "radar");
  expect(radars).toHaveLength(12);
  for (const r of radars) {
    expect(r.props.owned, `${r.name} must not be marked as owned`).toBe(false);
    expect(r.props.operator).toBe("NORAD");
    expect(r.last_heard, `${r.name} must not report a heartbeat`).toBeNull();
    // The approximation is declared in the data, not just in a comment, so a
    // consumer cannot mistake these for surveyed positions.
    expect(r.props.position_accuracy).toBe("approximate");
  }
});
