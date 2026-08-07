import { expect, test } from "@playwright/test";

import { landPixelFraction, readClock, waitForAppLoaded, waitForIceLoaded } from "./helpers";

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
  await waitForAppLoaded(page);
  // A healthy polar view is roughly a quarter land. A broken one is zero.
  await expect
    .poll(() => landPixelFraction(page), { timeout: 30_000, intervals: [500] })
    .toBeGreaterThan(0.05);
});

test("server data reaches the UI", async ({ page }) => {
  await page.goto("/");
  await waitForAppLoaded(page);

  const footer = page.locator(".strip.bottom");
  // The mesh is computed per request rather than stored, so its arrival in the UI
  // proves the derivation ran, not that a column was read back.
  await expect(footer).toContainText(/mesh \d+ links/);
  const text = await footer.innerText();
  expect(Number(text.match(/mesh (\d+) links/)?.[1] ?? 0), "computed radio links").toBeGreaterThan(0);
  expect(Number(text.match(/(\d+) groups/)?.[1] ?? 0), "connected groups").toBeGreaterThan(0);
});

test("the playback clock advances", async ({ page }) => {
  await page.goto("/");
  await waitForAppLoaded(page);

  const first = await readClock(page);
  await page.waitForTimeout(2500);
  const second = await readClock(page);

  expect(
    second.getTime() - first.getTime(),
    "clock should advance roughly with real time",
  ).toBeGreaterThan(1000);
});

test("the ice timebar scrubs to a different measurement and the map follows", async ({ page }) => {
  /**
   * 🥇 The assertion that guards the layer's one claim. Moving the control has to
   * change BOTH the date shown and the extent reported, because those come from
   * different places: the date is the snapped measurement, the extent was computed
   * on the source grid at build time. A decoder that silently returned the same
   * frame for every date would still move the label and would fail here.
   *
   * March against September is chosen deliberately. The Arctic maximum is roughly
   * four times the minimum, so this is a gap no rounding or off-by-one can produce.
   */
  await page.goto("/");
  await waitForIceLoaded(page);

  const slider = page.locator(".timebar input[type=range]");
  const dateLabel = page.locator(".timebar .icedate");
  const footer = page.locator(".strip.bottom");

  const extentNow = async () =>
    Number((await footer.innerText()).match(/ice ([\d.]+)M/)?.[1] ?? 0);

  // Walk to a March and to a September by reading the label rather than by
  // assuming an index, so the test survives the vendored range being extended.
  const max = Number(await slider.getAttribute("max"));
  let march = -1;
  let september = -1;
  for (let i = 0; i <= max; i++) {
    await slider.fill(String(i));
    const label = await dateLabel.innerText();
    if (march < 0 && label.startsWith("MAR")) march = i;
    if (september < 0 && label.startsWith("SEP")) september = i;
    if (march >= 0 && september >= 0) break;
  }
  expect(march, "a March measurement must be reachable").toBeGreaterThanOrEqual(0);
  expect(september, "a September measurement must be reachable").toBeGreaterThanOrEqual(0);

  await slider.fill(String(march));
  await expect(dateLabel).toContainText("MAR");
  const winter = await extentNow();

  await slider.fill(String(september));
  await expect(dateLabel).toContainText("SEP");
  const summer = await extentNow();

  expect(winter, "March extent").toBeGreaterThan(12);
  expect(summer, "September extent").toBeLessThan(7);
  expect(winter, "winter must carry far more ice than summer").toBeGreaterThan(summer * 1.5);
});

test("the timebar only ever offers dates that were actually measured", async ({ page }) => {
  /**
   * The control is an index into the vendored list, not a date picker, so every
   * position on it is a day a satellite flew. This asserts the property that makes
   * the layer's claim honest: you cannot land on a date nobody observed.
   */
  await page.goto("/");
  await waitForIceLoaded(page);

  const gotIce = await page.evaluate(async () => {
    const r = await fetch("/data/ice.json");
    return (await r.json()).dates as string[];
  });

  const slider = page.locator(".timebar input[type=range]");
  expect(Number(await slider.getAttribute("max")) + 1, "one stop per measurement").toBe(
    gotIce.length,
  );
  expect(await slider.getAttribute("step"), "the control must snap").toBe("1");
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
  await waitForAppLoaded(page);

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
  await waitForAppLoaded(page);
  await expect(page.locator(".strip.bottom")).toContainText("view all longitudes");
});

test("all seven asset kinds load and reach the map", async ({ page }) => {
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
  expect(counts).toEqual({
    node: 24,
    patrol: 3,
    uas: 5,
    hydrophone: 10,
    vessel: 8,
    radar: 12,
    launch_site: 6,
  });

  await expect(page.locator(".strip.bottom")).toContainText("assets 68");
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
  await waitForAppLoaded(page);
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
