import { PNG } from "pngjs";

import { expect, test, type Page } from "@playwright/test";

import { landPixelFraction, waitForAppLoaded, waitForIceLoaded } from "./helpers";

/**
 * Read the map canvas, cropped to the part of it that is actually map.
 *
 * 🔴 THE CROP IS LOAD-BEARING. `.map` is `inset: 0`, so a screenshot of it also contains
 * the header, the command bar, the timebar and the footer painted on top. Counting those
 * as map content is not a hypothetical: it made a checkbox's own pixels and an input
 * focus ring dominate a measurement of the mesh layer, and produced a confident and
 * completely wrong conclusion about which MapLibre API was broken.
 */
const CROP = { x0: 0, x1: 1440, y0: 60, y1: 760 };

async function mapImage(page: Page): Promise<PNG> {
  return PNG.sync.read(await page.locator(".map").screenshot());
}

/** Pixels differing between two frames of the SAME run. Not a golden image: both sides
 *  are captured here, so driver differences cancel and only a real change registers. */
function differingPixels(a: PNG, b: PNG): number {
  let n = 0;
  for (let y = CROP.y0; y < Math.min(CROP.y1, a.height); y++) {
    for (let x = CROP.x0; x < Math.min(CROP.x1, a.width); x++) {
      const i = (a.width * y + x) << 2;
      if (
        Math.abs(a.data[i] - b.data[i]) > 6 ||
        Math.abs(a.data[i + 1] - b.data[i + 1]) > 6 ||
        Math.abs(a.data[i + 2] - b.data[i + 2]) > 6
      ) {
        n++;
      }
    }
  }
  return n;
}

/** How many pixels carry a given colour, within a tolerance. */
function colourCount(png: PNG, [tr, tg, tb]: [number, number, number], tol = 5): number {
  let n = 0;
  for (let y = CROP.y0; y < Math.min(CROP.y1, png.height); y++) {
    for (let x = CROP.x0; x < Math.min(CROP.x1, png.width); x++) {
      const i = (png.width * y + x) << 2;
      if (
        Math.abs(png.data[i] - tr) <= tol &&
        Math.abs(png.data[i + 1] - tg) <= tol &&
        Math.abs(png.data[i + 2] - tb) <= tol
      ) {
        n++;
      }
    }
  }
  return n;
}

/** Zoom into the central cluster, where assets and links are dense enough to measure. */
async function zoomToCluster(page: Page): Promise<void> {
  await page.mouse.move(700, 430);
  for (let i = 0; i < 5; i++) {
    await page.mouse.wheel(0, -400);
    await page.waitForTimeout(400);
  }
  await page.waitForTimeout(3000);
}

/**
 * Does the console actually work? Questions no other test in this repo can answer.
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

  const picker = page.locator(".timebar .icepick");
  const footer = page.locator(".strip.bottom");
  // The select IS the readout now, so what is shown is asserted on its value rather than
  // on a second label that would only ever repeat it.
  const shown = async () =>
    await picker.locator("option:checked").innerText();

  const extentNow = async () =>
    Number((await footer.innerText()).match(/ice ([\d.]+)M/)?.[1] ?? 0);

  // Found by reading the option text rather than by assuming an index, so this survives
  // the vendored range being extended or a month being missing from a year.
  const options = await picker.locator("option").allTextContents();
  const values = await picker.locator("option").evaluateAll((els) =>
    els.map((el) => (el as HTMLOptionElement).value),
  );
  const march = values[options.findIndex((t) => t.startsWith("MAR"))];
  const september = values[options.findIndex((t) => t.startsWith("SEP"))];
  expect(march, "a March measurement must be selectable").toBeTruthy();
  expect(september, "a September measurement must be selectable").toBeTruthy();

  // Choosing is free; GO is what draws it. Selecting without pressing GO must change
  // nothing on the map, which is the whole reason the button exists.
  const go = page.locator(".timebar .icego");
  const extentBefore = await extentNow();
  await picker.selectOption(march);
  expect(await extentNow(), "choosing a month must not draw it yet").toBe(extentBefore);

  await go.click();
  expect(await shown(), "the control must show the March measurement").toContain("MAR");
  const winter = await extentNow();

  await picker.selectOption(september);
  await go.click();
  expect(await shown(), "the control must show the September measurement").toContain("SEP");
  const summer = await extentNow();

  expect(winter, "March extent").toBeGreaterThan(12);
  expect(summer, "September extent").toBeLessThan(7);
  expect(winter, "winter must carry far more ice than summer").toBeGreaterThan(summer * 1.5);
});

test("the timebar only ever offers dates that were actually measured", async ({ page }) => {
  /**
   * 🥇 THE ASSERTION THAT KEEPS THE LAYER'S CLAIM HONEST. Every option is a day a
   * satellite flew, so there is no way to land on a date nobody observed.
   *
   * Stronger now than when this was a slider. A range input could only be checked
   * structurally, one stop per measurement, and it was safe because it happened to be an
   * index. A list can be checked on its actual VALUES: every option is compared against
   * the vendored date set, so an invented date would have to appear in both places.
   */
  await page.goto("/");
  await waitForIceLoaded(page);

  const gotIce = await page.evaluate(async () => {
    const r = await fetch("/data/ice.json");
    return (await r.json()).dates as string[];
  });

  const picker = page.locator(".timebar .icepick");
  const values = await picker.locator("option").evaluateAll((els) =>
    els.map((el) => (el as HTMLOptionElement).value),
  );

  expect(values.length, "one option per measurement").toBe(gotIce.length);
  expect(values.slice().sort(), "every option must be a measured date").toEqual(
    gotIce.slice().sort(),
  );
  // Grouped by year, which is what makes 55 options readable rather than a scroll.
  expect(
    await picker.locator("optgroup").count(),
    "options are grouped by year",
  ).toBeGreaterThan(1);
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

test("all nine asset kinds load and reach the map", async ({ page }) => {
  /**
   * The asset picture is the point of the application, so its arrival is asserted
   * end to end rather than inferred from the map not being blank.
   *
   * Counts are exact, not "greater than zero". A seed that silently drops a kind
   * would still satisfy a loose assertion, and a missing kind is invisible on a
   * globe covered in dots.
   *
   * 🔑 SEEDED ASSETS ONLY, and that is what keeps the exactness affordable. The command
   * layer can create real assets, and one created during a demo is not a defect in the
   * seed. Counting everything meant a single operator-placed hydrophone turned this red,
   * and the honest options were to filter or to weaken the assertion. Filtering keeps the
   * thing the test is actually for: proof that the seeded world arrived whole.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const seeded = body.entities.filter((a: { created_by: string }) => a.created_by === "seed");
  const counts: Record<string, number> = {};
  for (const a of seeded) counts[a.kind] = (counts[a.kind] ?? 0) + 1;
  expect(counts).toEqual({
    node: 24,
    patrol: 3,
    uas: 5,
    hydrophone: 10,
    vessel: 9,
    radar: 12,
    launch_site: 6,
    aircraft: 4,
    ground_party: 3,
  });

  // The footer counts everything on the map, seeded or placed, which is correct for a
  // status strip. So it is asserted against the total rather than against the seed.
  await expect(page.locator(".strip.bottom")).toContainText(`assets ${body.entities.length}`);
});

test("the non-broadcasting contacts are surfaced, and one is held by nothing", async ({
  page,
}) => {
  /**
   * 🥇 The most important assertion about the data. A contact held without an AIS
   * broadcast is the case the whole system exists for. The count stays small by design:
   * one anomaly reads as a fluke, five reads as a game, and if a seed change ever made it
   * zero the demo would lose its point silently.
   *
   * 🔑 THE UNHELD CONTACT IS ASSERTED SEPARATELY, ON PURPOSE. Most contacts name the
   * sensor holding them, which is what makes an unidentified track actionable rather than
   * merely alarming: somebody has it, and you can say who. One is held by nothing that
   * reaches us, and that is an honest answer rather than a gap in the data. Asserting
   * "every contact names a sensor" would have been the tidier test and would have made
   * that case impossible to seed.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const dark = body.entities.filter(
    (a: { ais_reporting: boolean | null }) => a.ais_reporting === false,
  );
  expect(dark.length, "there must be contacts to find").toBeGreaterThan(1);
  expect(dark.length, "a display where everything is unidentified says nothing").toBeLessThan(6);

  // `held_by` is the current name and `first_detected_by` was the previous one. Reading
  // both means a rename cannot quietly turn this into a check on `undefined`.
  const heldBy = (d: { props: Record<string, unknown> }) =>
    d.props.held_by ?? d.props.first_detected_by;

  const named = dark.filter((d: { props: Record<string, unknown> }) => heldBy(d));
  expect(named.length, "most contacts must name the sensor holding them").toBeGreaterThan(1);
  for (const d of named) {
    expect(heldBy(d), `${d.name} should name a sensor`).toMatch(/^(hyd|node)-/);
  }

  expect(
    dark.length - named.length,
    "exactly one contact is held by nothing that reaches us",
  ).toBe(1);

  await expect(page.locator(".strip.bottom")).toContainText(
    `not broadcasting ${dark.length}`,
  );
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

test("asset names are drawn on the map, and nothing is fetched to draw them", async ({ page }) => {
  /**
   * 🥇 THE TEST THAT GUARDS THE WHOLE LABEL APPROACH. Text on this map is rasterised to
   * images rather than drawn from a glyph endpoint, because a `text-field` needs `glyphs`
   * and the default for that is a remote URL. Two things therefore have to hold, and they
   * fail in opposite directions.
   *
   *   1. The labels actually draw. The label text colour is one MapLibre can only produce
   *      by painting a label: it is not the land fill, not the ice ramp, and far enough
   *      from the vessel icon colour to survive a tight tolerance.
   *   2. Nothing left the origin to do it. If someone "fixes" the labels by adding a
   *      glyphs URL or a web font, this goes red, which is the entire point.
   */
  // Collected during the run and classified afterwards. Classifying inside the listener
  // was the first attempt and it was wrong: the very first request fires while the page
  // is still `about:blank`, whose origin matches nothing, so the navigation flagged
  // itself.
  const requested: string[] = [];
  page.on("request", (r) => requested.push(r.url()));

  await page.goto("/");
  await waitForAppLoaded(page);
  await page.waitForTimeout(3000);

  // 🔴 POLLED, NOT SLEPT ON. A fixed wait was wrong here and the reason is worth keeping:
  // "assets 76" appears as soon as the entity fetch resolves, but the MAP is still busy
  // building the ice layer, which is 168,000 measured cells. Labels paint after that, so a
  // three second sleep measured an empty canvas and reported zero labels on a display that
  // draws them correctly a moment later. Polling asks the real question, "do they appear",
  // instead of the wrong one, "are they there yet at exactly three seconds".
  await expect
    .poll(async () => colourCount(await mapImage(page), [201, 214, 226]), {
      timeout: 40_000,
      intervals: [1000],
    })
    .toBeGreaterThan(400);

  const origin = new URL(page.url()).origin;
  const offOrigin = requested.filter((u) => new URL(u).origin !== origin);
  expect(offOrigin, "the map must fetch nothing off-origin, glyphs and fonts included").toEqual([]);
});

test("hiding the mesh removes the link lines, and putting it back restores them", async ({
  page,
}) => {
  /**
   * The mesh is one layer among several now rather than the subject of the display, so it
   * has to be possible to get it out of the way.
   *
   * Asserted by diffing frames captured in this run, never against a stored image. That
   * distinction matters: a reference screenshot of a WebGL globe fails on driver
   * differences and answers "something changed" when the question is "did the layer go".
   * Comparing a frame to another frame from the same browser asks only about behaviour.
   *
   * Zoomed in first, because at the opening pole-centred view the links are a handful of
   * thin antialiased pixels and a real regression would hide inside the noise.
   */
  // ?live=off stops the 5s position poll. These assertions compare one frame to another,
  // and the server advances assets it can hear, so a moving world would make a real
  // difference indistinguishable from ordinary motion.
  // 🔒 SKIP WITH A REASON RATHER THAN PASS, when there is nothing on screen to hide.
  // A link to an asset we cannot hear is not drawn, so on a stale seed where almost
  // everything is unreachable the map holds no links at all and this assertion has no
  // subject. Passing would be a lie and a bare pixel-count failure would send the next
  // person hunting through the toggle. Same rule the CI groups follow: a check that
  // cannot run says so, loudly, and never reads as a clean bill.
  await page.goto("/?live=off");
  await waitForAppLoaded(page);
  const drawable = await page.evaluate(async () => {
    const [ents, mesh] = await Promise.all([
      fetch("/api/entities").then((r) => r.json()),
      fetch("/api/mesh").then((r) => r.json()),
    ]);
    const unreachable = new Set(
      ents.entities
        .filter((a: Record<string, unknown>) =>
          a.reachable !== undefined && a.reachable !== null
            ? !a.reachable
            : a.flag !== undefined && a.flag !== null
              ? a.flag === "overdue"
              : a.status === "silent",
        )
        .map((a: { id: string }) => a.id),
    );
    return mesh.links.filter(
      (l: { a: string; b: string }) => !unreachable.has(l.a) && !unreachable.has(l.b),
    ).length;
  });
  test.skip(
    drawable === 0,
    "no mesh link has both endpoints reachable, so none are drawn and there is nothing " +
      "to toggle. The seeded world has aged out; reseed and this runs again.",
  );

  await page.waitForTimeout(3000);
  await zoomToCluster(page);

  const box = page.locator(".toggle input[type=checkbox]");
  await expect(box, "the mesh is on by default").toBeChecked();

  const on = await mapImage(page);
  await box.uncheck();
  await page.waitForTimeout(2500);
  const off = await mapImage(page);
  await box.check();
  await page.waitForTimeout(2500);
  const back = await mapImage(page);

  const removed = differingPixels(on, off);
  expect(removed, "hiding the mesh must visibly remove the link lines").toBeGreaterThan(40);
  expect(
    differingPixels(on, back),
    "showing it again must restore the same picture",
  ).toBeLessThan(removed / 5);

  // Hiding the lines hides the lines. It does not change what the mesh IS, and the footer
  // is the readout that must keep saying so.
  await expect(page.locator(".strip.bottom")).toContainText(/mesh \d+ links/);
});

test("no line is drawn until a command asks for one, and it clears on the next", async ({
  page,
}) => {
  /**
   * 🥇 A LINE ON THIS MAP MEANS SOMEBODY ASKED A QUESTION.
   *
   * Eleven of the sixty-eight assets carry a seeded route, and those used to be drawn
   * permanently. That made the display open with trails behind vessels nobody had asked
   * about, and it meant the one command that produces a line would have produced no
   * visible change, because lines were already scenery.
   *
   * ⚠️ The response is MOCKED, deliberately, and that is not a shortcut. This asserts the
   * rendering half of the contract: given a `track` effect, does a trail appear, and does
   * the next command take it away. Whether the server produces that effect is the other
   * lane's test to write, and mocking here means this one keeps working while their
   * history module is still landing.
   */
  await page.goto("/?live=off");
  await waitForAppLoaded(page);
  await page.waitForTimeout(3000);

  const trackColour: [number, number, number] = [199, 146, 234];
  // Tolerance 12 absorbs the antialiased edge of a thin line. Safe here because this
  // violet is the only one on the display: nothing else is within reach of it.
  const TOL = 12;
  const atRest = colourCount(await mapImage(page), trackColour, TOL);
  // 🥇 THE CLAIM ETHAN ASKED FOR, ASSERTED FIRST: at rest, before anybody has typed
  // anything, there is no trail on this map at all.
  expect(atRest, "nothing may draw a trail before a command asks for one").toBeLessThan(20);

  // A long track through the archipelago, well inside the opening view.
  const coordinates: [number, number][] = [];
  for (let i = 0; i <= 40; i++) coordinates.push([-108 + i * 0.35, 69.5 + i * 0.12]);

  let reply: object = {
    ok: true,
    summary: "4 days of history for Daymark 03",
    tier: "parser",
    results: [],
    ui_effects: { track: { id: "uas-daymark-03", coordinates } },
  };
  await page.route("**/api/command", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(reply) }),
  );

  const input = page.locator(".cmdinput");
  await input.fill("show me 4 days of history for Daymark 03");
  await input.press("Enter");
  await expect(page.locator(".activity")).toContainText("4 days of history");
  await page.waitForTimeout(2500);

  const withTrack = colourCount(await mapImage(page), trackColour, TOL);
    // A 2px line at the opening zoom is a modest pixel count, so the bar is set from a
  // measurement (77) rather than from optimism. Zero and 77 are not close.
  expect(withTrack, "asking for history must draw a trail").toBeGreaterThan(atRest + 40);

  // The next command carries no track, so the previous answer must come off the map
  // rather than accumulate under the new one.
  reply = { ok: true, summary: "46 links up across 9 groups", tier: "parser", results: [] };
  await input.fill("mesh status");
  await input.press("Enter");
  await expect(page.locator(".activity")).toContainText("46 links up");
  await page.waitForTimeout(2500);

  const cleared = colourCount(await mapImage(page), trackColour, TOL);
  expect(cleared, "the next command must clear the trail").toBeLessThan(atRest + 20);
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
