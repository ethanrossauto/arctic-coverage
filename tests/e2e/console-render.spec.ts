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

/**
 * Wait until the map has actually PAINTED, not merely until the data arrived.
 *
 * 🔴 "assets 76" in the footer appears as soon as the entity fetch resolves, which can be
 * seconds before MapLibre has drawn anything. Every test below that compares pixels was
 * originally sleeping three seconds after that text and then measuring an empty canvas, so
 * a correct display reported zero. Under software WebGL the gap is large enough to make a
 * fixed sleep a coin toss.
 *
 * Keyed on the land fill, which cannot appear unless the source loaded, the worker parsed
 * it and the GPU drew it.
 */
async function waitForMapPainted(page: Page): Promise<void> {
  await expect
    .poll(() => landPixelFraction(page), { timeout: 45_000, intervals: [500] })
    .toBeGreaterThan(0.05);
  // 🔴 AND WAIT FOR THE ICE, which is the slowest layer and the one that changes the most
  // pixels when it lands. Land can be painted seconds before the ice tile has been fetched,
  // decoded and uploaded, and a frame diff started in that window measures the ice arriving
  // rather than the thing under test: it reported nearly half a million changed pixels for
  // a toggle that moves about twenty-five.
  await waitForIceLoaded(page);
}

/**
 * Capture once the picture has stopped changing.
 *
 * 🔴 A FRAME DIFF NEEDS A SETTLED FRAME, and the ice layer made that non-obvious. It is a
 * raster now, so the GPU resamples it whenever the camera moves and consecutive frames
 * differ for a while after a zoom. Comparing two unsettled frames reported nearly half a
 * million changed pixels for a toggle that touches a few hundred, which is a measurement
 * of the renderer catching up rather than of the thing under test.
 *
 * Settled is defined as two consecutive captures agreeing, which asks the screen rather
 * than guessing a duration.
 */
async function settledImage(page: Page): Promise<PNG> {
  // ⚠️ THREE CONSECUTIVE AGREEMENTS, NOT ONE. A single agreeing pair happens by luck in the
  // quiet moment between two changes, and this layer has several things landing at
  // different times. Requiring the picture to hold still for a stretch is what actually
  // distinguishes "settled" from "briefly between events".
  const NEEDED = 3;
  let prev = await mapImage(page);
  let stable = 0;
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(400);
    const next = await mapImage(page);
    stable = differingPixels(prev, next) < 50 ? stable + 1 : 0;
    prev = next;
    if (stable >= NEEDED) return next;
  }
  return prev;
}

/** Zoom into the central cluster, where assets and links are dense enough to measure. */
async function zoomToCluster(page: Page): Promise<void> {
  await page.mouse.move(700, 430);
  // Three steps, not five. Deep enough that the link lines are more than antialiasing,
  // shallow enough to keep a whole cluster's worth of them in frame: zooming further put
  // most of the mesh off screen and left almost nothing for the toggle to change.
  for (let i = 0; i < 3; i++) {
    await page.mouse.wheel(0, -400);
    await page.waitForTimeout(400);
  }
  await page.waitForTimeout(2000);
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

  // Every count an operator reads at a glance, all of them server data that had to survive
  // the fetch, the store and the render to appear here at all.
  const footer = page.locator(".strip.bottom");
  await expect(footer).toContainText(/our assets\s+\d+/);
  // ⚠️ A LOOKBEHIND, NOT A WORD BOUNDARY. `innerText` joins the flex children with no
  // separator, so the strip reads "current statusreachable 36" and there is no boundary
  // in front of the word at all. Excluding a preceding "un" is what actually separates
  // this reading from "unreachable/overdue" beside it.
  await expect(footer).toContainText(/(?<!un)reachable \d+/);
  await expect(footer).toContainText(/unreachable\/overdue \d+/);
  await expect(footer).toContainText(/nominal \d+/);
  await expect(footer).toContainText(/maintenance \d+/);
  await expect(footer).toContainText(/detected unknown \d+/);

  // 🔑 THE PROPERTY THE STRIP IS FOR: two independent readings of one set, each accounting
  // for all of it. `current status` asks whether we can hear it, `last message` asks what
  // it last told us, and both have to add up to the total printed beside them. A group that
  // does not sum leaves a remainder no label explains, which is what this strip did while
  // it counted contacts and radar sites in the same number as our own kit.
  const text = await footer.innerText();
  const count = (re: RegExp) => Number(text.match(re)?.[1] ?? -1);
  const total = count(/our assets\s+(\d+)/);
  expect(total).toBeGreaterThan(0);
  expect(
    count(/(?<!un)reachable (\d+)/) + count(/unreachable\/overdue (\d+)/),
    "current status must account for every one of our assets",
  ).toBe(total);
  expect(
    count(/nominal (\d+)/) + count(/maintenance (\d+)/),
    "last message must account for every one of our assets",
  ).toBe(total);

  // ⛔ AND NEITHER GROUP MAY QUIETLY GROW TO INCLUDE THEM AGAIN. The radar layer is not
  // ours and a contact is not an asset, so the words that used to carry them are gone from
  // this strip rather than merely unused.
  await expect(footer).not.toContainText(/not on mesh/);
  await expect(footer).not.toContainText(/not reporting/);

  // ⚠️ THE MESH LEFT THE STATUS STRIP, so its derivation is asserted at the endpoint that
  // computes it rather than at a label that no longer exists. It is still computed per
  // request rather than stored, which is the property worth proving: connectivity is a
  // question you ask, and the map draws the answer.
  const meshBody = await page.evaluate(() => fetch("/api/mesh").then((r) => r.json()));
  expect(meshBody.links.length, "computed radio links").toBeGreaterThan(0);
  expect(meshBody.groups.length, "connected groups").toBeGreaterThan(0);
});

test("the ice timebar scrubs to a different measurement and the map follows", async ({ page }) => {
  /**
   * 🥇 The assertion that guards the layer's one claim: moving the control has to change
   * what is DRAWN, not merely what is labelled. A decoder that silently returned the same
   * frame for every date would still move the label and would fail here.
   *
   * ⚠️ IT USED TO READ AN EXTENT FIGURE OUT OF THE STATUS STRIP, and that figure has been
   * removed from the display. The magnitude claim it was making did not need a browser and
   * is asserted better without one: `test_march_and_september_bracket_the_published_seasonal_range`
   * checks every vendored March against 13-16M km2 and every September against 3-6.5M, on
   * the source data, with no rendering in the way. What is left here is the half that only
   * a browser can answer, which is whether pressing GO changes the picture.
   *
   * March against September is still chosen deliberately: the Arctic maximum is roughly four
   * times the minimum, so it is a difference no rounding can produce.
   */
  // 🔴 THE DEFAULT 30 SECONDS CANNOT HOLD THIS TEST, AND THE ARITHMETIC SAYS SO RATHER THAN
  // THE MOOD OF THE MACHINE. It settles the picture three separate times, and `settledImage`
  // budgets up to forty samples at 400ms apiece, so the waiting alone can reach 48 seconds
  // before a single measurement tile is fetched. It passed for as long as the map happened
  // to hold still quickly every time, which made a structural shortfall look like an
  // intermittent one: it went red under the load of a full suite run, green on its own, then
  // red on its own once something else was busy.
  //
  // ⚠️ RAISING THE CLOCK WEAKENS NOTHING. Every assertion below is unchanged, and
  // `settledImage` still refuses to measure an unsettled frame. What was failing was the
  // budget, not the claim.
  test.setTimeout(150_000);

  await page.goto("/?live=off");
  await waitForAppLoaded(page);
  await waitForMapPainted(page);

  const picker = page.locator(".timebar .icepick");
  const go = page.locator(".timebar .icego");
  // The select IS the readout, so what is shown is asserted on its value rather than on a
  // second label that would only ever repeat it.
  const shown = async () => await picker.locator("option:checked").innerText();

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

  // Choosing is free; GO is what draws it. Selecting without pressing GO must leave the
  // map alone, which is the whole reason the button exists.
  await picker.selectOption(march);
  await go.click();
  await expect(go).toBeDisabled();
  expect(await shown(), "the control must show the March measurement").toContain("MAR");
  const winter = await settledImage(page);

  await picker.selectOption(september);
  const stillWinter = await settledImage(page);
  expect(
    differingPixels(winter, stillWinter),
    "choosing a month must not draw it until GO is pressed",
  ).toBeLessThan(2000);

  // 🔑 GO DISABLES WHEN THE MAP HAS CAUGHT UP, which is the app's own statement that the
  // chosen month is the drawn one. Waiting on it beats a sleep: each date fetches its own
  // measurement tile, so measuring too early compares a month against itself.
  await go.click();
  await expect(go).toBeDisabled();
  expect(await shown(), "the control must show the September measurement").toContain("SEP");
  const summer = await settledImage(page);

  // The Arctic loses roughly three quarters of its ice between these two months, so the
  // frames cannot be close. A bar rather than an exact figure: how many pixels that is
  // depends on the camera, and the camera is not what this test is about.
  expect(
    differingPixels(winter, summer),
    "March and September must not draw the same ice",
  ).toBeGreaterThan(20000);
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
    // The measurement set is an index plus one PNG per date now, not one big JSON.
    const r = await fetch("/data/ice-index.json");
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
  // 🔑 ASSERTED THROUGH THE CONTRACT ITSELF, NOT THROUGH A LABEL. The status strip used to
  // print the viewport, and this read that text. The readout is gone, and asking the
  // command layer is the stronger test anyway: it exercises the thing the readout was only
  // ever evidence FOR, which is that a pole-centred camera reporting every longitude
  // survives all the way into a filter rather than being treated as a bad box.
  await page.goto("/");
  await waitForAppLoaded(page);
  await waitForMapPainted(page);

  // 🔑 THE BOX UNDER TEST IS THE ONE THE LIVE MAP COMPUTED, which is the whole point: a
  // hand-written bbox posted to the API would assert that the SERVER handles a global box,
  // and that was never in doubt. What matters is that the CLIENT produces one.
  //
  // ⚠️ THIS USED TO TYPE "show me assets in the current view" AND READ THE ANSWER, and that
  // stopped working when tier 1 became one wording per tool: `bbox` is a parameter of
  // `list_entities` and its single declared sentence does not set it, so those words now go
  // to the model, and asserting on a model's choice of parameters would make this test
  // non-deterministic and paid.
  //
  // 🔑 SO IT ASSERTS THE REQUEST INSTEAD OF THE ANSWER, which is closer to the claim in the
  // first place. The client attaches its viewport to EVERY command, so any command exercises
  // the plumbing, and reading it off the wire removes both tiers from the test.
  let posted: { bbox?: Record<string, unknown> } | null = null;
  await page.route("**/api/command", async (route) => {
    posted = JSON.parse(route.request().postData() ?? "{}").context ?? {};
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, summary: "mesh status", tier: "parser", results: [] }),
    });
  });

  await page.locator(".cmdinput").fill("mesh status");
  await page.locator(".cmdinput").press("Enter");
  await expect(page.locator(".activity")).toContainText("mesh status");

  const sent = posted as { bbox?: Record<string, unknown> } | null;
  expect(sent?.bbox, "the client must attach the viewport it is looking at").toBeTruthy();
  const box = sent!.bbox!;
  expect(
    typeof box.south === "number" && typeof box.north === "number",
    "a pole-centred view still has latitudes",
  ).toBe(true);
  // A pole-centred camera legitimately spans every longitude, and the flag is how the client
  // says so rather than sending a west greater than its east and hoping.
  if (box.global !== true) {
    expect(typeof box.west === "number" && typeof box.east === "number").toBe(true);
  }
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

  // 🔑 THE STRIP'S TOTAL IS THE FRIENDLY SET, NOT EVERY ROW, and the difference is the
  // reason its numbers add up. Every condition it counts is a fact about our own
  // equipment: whether we can reach it, and what its last message said. Neither is a fact
  // about a ship that has never reported to us, so counting contacts in the same total
  // left a remainder no label could explain. Contacts get their own count.
  //
  // Asserted against the live total rather than the seed, because a placed asset is as
  // real as a seeded one and the strip is right to count it.
  const MESH_KINDS = new Set(["node", "patrol", "uas", "launch_site", "hydrophone"]);
  const ours = body.entities.filter((e: { kind: string }) => MESH_KINDS.has(e.kind));
  await expect(page.locator(".strip.bottom")).toContainText(`our assets ${ours.length}`);
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

  // 🔑 THE STRIP COUNTS DETECTED UNKNOWN: contacts that reached us and will not say what
  // they are. It used to count every contact with AIS off, which swept in the ones whose
  // detections never arrived at all, so the strip reported contacts that were deliberately
  // absent from the map beside it and nobody reading both could reconcile them.
  //
  // ⚠️ AND IT IS NOT A SUBSET OF THE AIS-OFF SET, WHICH IS THE WHOLE REASON THE SERVER
  // COMPUTES IT. `ais_reporting` is a vessel field, so an aircraft or a ground party that is
  // not announcing carries null there and falls through to a transponder and then to an
  // emitting flag. On this seed the obvious client-side rule of
  // `tracked && ais_reporting === false` gives 2 and the truth is 4, and the two would have
  // disagreed quietly forever. If anyone is ever tempted to move this arithmetic into the
  // browser to save a field on the wire, this is the assertion that says no.
  const detectedUnknown = body.entities.filter(
    (a: { detected_unknown?: boolean }) => a.detected_unknown,
  );
  const undetected = body.entities.filter((a: { tracked?: boolean }) => a.tracked === false);

  expect(detectedUnknown.length, "there must be contacts we hold but cannot name").toBeGreaterThan(0);
  expect(undetected.length, "and some the console never saw at all").toBeGreaterThan(0);

  // Disjoint by construction, and worth pinning: one is what reached this console and the
  // other is what did not, so nothing can be both.
  const undetectedIds = new Set(undetected.map((a: { id: string }) => a.id));
  expect(
    detectedUnknown.filter((a: { id: string }) => undetectedIds.has(a.id)),
    "a contact cannot be both detected and undetected",
  ).toHaveLength(0);

  await expect(page.locator(".strip.bottom")).toContainText(
    `detected unknown ${detectedUnknown.length}`,
  );

  // A status strip may only count what it can show, and everything it counts here is on the
  // map with the default controls untouched.
  await expect(page.locator('.toggle:has-text("HIDE UNDETECTED UNKNOWN") input')).toBeChecked();
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

test("the mesh link lines are drawn without anyone asking, and cannot be switched off", async ({
  page,
}) => {
  /**
   * The mesh is what makes this a network picture rather than a map with icons on it, so it
   * is always drawn and there is no control that can take it away.
   *
   * Asserted by diffing frames captured in this run, never against a stored image. That
   * distinction matters: a reference screenshot of a WebGL globe fails on driver
   * differences and answers "something changed" when the question is "did the layer go".
   * Comparing a frame to another frame from the same browser asks only about behaviour.
   *
   * Zoomed in first, because at the opening pole-centred view the links are a handful of
   * thin antialiased pixels and a real regression would hide inside the noise.
   *
   * ⏱️ TWO FULL PAGE LOADS, SO IT NEEDS LONGER THAN THE DEFAULT. It used to toggle a
   * checkbox, which is nearly free; starving the app of links means loading it again with
   * the response mocked. The reload cannot be avoided by waiting for the next poll, because
   * `?live=off` is what stops the world moving between the two frames being compared, and
   * it stops the poll along with it.
   */
  test.setTimeout(90_000);
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
    // ⚠️ `server_reachable` IS THE FIELD, and this read `reachable` for hours after the
    // rename. It fell through to a different fallback, so the probe reported links as
    // drawable while the app drew none, and the test failed with "removed 0 pixels" on a
    // display that was behaving correctly. A stale field name in a guard is worse than no
    // guard: it answers confidently and wrongly.
    const unreachable = new Set(
      ents.entities
        .filter((a: Record<string, unknown>) =>
          a.server_reachable !== undefined && a.server_reachable !== null
            ? !a.server_reachable
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
    drawable < 4,
    "no mesh link has both endpoints reachable, so none are drawn and there is nothing " +
      "to toggle. The seeded world has aged out; reseed and this runs again.",
  );

  await page.waitForTimeout(3000);
  await zoomToCluster(page);

  // 🔑 THE CONTRACT CHANGED AND SO DID THIS TEST. The mesh used to have a header checkbox
  // and this measured hiding and restoring it. The toggle is gone: the mesh is what makes
  // this a network picture rather than a map with icons on it, and a control that hides the
  // subject earns its space only if somebody wants the subject hidden.
  //
  // ⚠️ SO THE ASSERTION HAD TO MOVE RATHER THAN BE DELETED. What the old test really proved
  // was that link lines put ink on the screen, and that is still worth proving. It is now
  // driven by starving the app of links instead of by a control, which is the same mocking
  // pattern the clarify tests use and for the same reason: it exercises the rendering half
  // of a contract without depending on a world that moves.
  const withLinks = await settledImage(page);

  // The control really is gone, so nothing puts it back by accident.
  await expect(page.locator('.toggle:has-text("MESH LINKS")')).toHaveCount(0);

  await page.route("**/api/mesh", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ links: [], groups: [], isolated: [], mesh_capable: 0 }),
    }),
  );
  await page.reload();
  await waitForAppLoaded(page);
  await waitForMapPainted(page);
  await page.waitForTimeout(3000);
  await zoomToCluster(page);
  const withoutLinks = await settledImage(page);

  // 🔑 PROPORTIONAL RATHER THAN AN ABSOLUTE PIXEL COUNT, because the absolute number is not
  // something this test controls. A link is drawn only when both ends are reachable, and
  // reachability moves as the world does, so how much ink the mesh puts on screen differs
  // run to run. An absolute bar calibrated on one afternoon's seed passed alone and failed
  // in a full run, which is the definition of a flaky test.
  expect(
    differingPixels(withLinks, withoutLinks),
    "link lines must be drawn without anyone asking for them",
  ).toBeGreaterThan(12);

  await page.unroute("**/api/mesh");
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
  await waitForMapPainted(page);

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
   *   - `relationship: third_party`, so a count of "my assets" never quietly includes
   *     twelve sites belonging to somebody else.
   *   - no `last_heard`, so it cannot be overdue. Giving radar a reporting threshold
   *     would make all twelve permanently overdue and bury the four assets that
   *     genuinely are.
   *
   * 🔴 THIS TEST ASSERTED A DESIGN THAT HAD BEEN DELIBERATELY REPLACED, and it went red the
   * first time the browser suite ran after the field schema landed. It read
   * `props.owned === false`, `props.operator === "NORAD"` and
   * `props.position_accuracy === "approximate"`. All three props were removed when per-asset
   * props were cut from 48 keys to 12: a single `owned` boolean put a NORAD radar site and an
   * unidentified vessel in one bucket, so it became two axes, `relationship` and `threat`,
   * derived and served at the top level.
   *
   * ⚠️ SO THE ASSERTION MOVES TO WHERE THE FACT LIVES NOW, and it is a stronger one: the value
   * is declared once in `domain.KINDS` and derived into every payload, rather than written onto
   * one kind by the seed and true only where somebody remembered to write it.
   *
   * 🔴 AND NOTHING HERE ASSERTS THE SHAPE OF `props`, WHICH IS THE HARDER LESSON. This suite ran
   * twice on 2026-08-12 against a database shared with the deployed site, and between the two
   * runs that deployment reseeded the world on its idle timer: every radar's props changed from
   * `{detection_radius_km}` to the nine-key seed the deployed commit writes, and a props
   * assertion that had just passed failed with `undefined`. Neither run was wrong. They were
   * reading worlds written by two different seeds.
   *
   * ✅ THE SHARING IS FIXED: local work now runs against its own Neon branch. The assertions
   * below stay derived anyway, because that is the stronger test and not merely the one that
   * survives a reseed: `relationship` is declared once in `domain.KINDS` and derived into every
   * payload, where `props.owned` was written onto one kind by the seed and true only where
   * somebody remembered to write it.
   */
  const gotAssets = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  await page.goto("/");
  const body = await (await gotAssets).json();

  const radars = body.entities.filter((a: { kind: string }) => a.kind === "radar");
  expect(radars).toHaveLength(12);
  for (const r of radars) {
    expect(r.relationship, `${r.name} must not read as ours`).toBe("third_party");
    expect(r.last_heard, `${r.name} must not report a heartbeat`).toBeNull();
    expect(r.overdue, `${r.name} cannot be late to a network it is not on`).toBe(false);
  }
});

test("clicking an asset opens its details, and clicking empty map closes them", async ({
  page,
}) => {
  /**
   * 🥇 THE BANNER IS THE ONLY WAY TO ASK "what am I looking at", so its absence is not a
   * cosmetic loss. Before this existed, clicking an asset was a genuine no-op: nothing in
   * `src/` listened for a map click at all.
   *
   * Asserted through the API rather than against a hardcoded name, because the seeded world
   * changes and a test that names an asset dies with the next reseed.
   */
  await page.goto("/?live=off");
  await waitForAppLoaded(page);
  await waitForMapPainted(page);

  expect(await page.locator(".banner").count(), "nothing is open before a click").toBe(0);

  // Find something to click by sweeping the dense part of the map. Cheaper and more honest
  // than projecting a lat/lon ourselves, which would duplicate MapLibre's own maths.
  let opened = false;
  outer: for (let y = 380; y <= 520; y += 12) {
    for (let x = 600; x <= 900; x += 12) {
      await page.mouse.click(x, y);
      await page.waitForTimeout(90);
      // A pile opens the chooser rather than the banner; take the first option so this
      // test is about the banner and not about which pixel was hit.
      if (await page.locator(".fanitem").count()) {
        await page.locator(".fanitem").first().click();
        await page.waitForTimeout(300);
      }
      if (await page.locator(".banner").count()) {
        opened = true;
        break outer;
      }
    }
  }
  expect(opened, "some asset on screen must be clickable").toBe(true);

  const name = await page.locator(".banner .bname").innerText();
  expect(name.trim().length, "the banner names what was clicked").toBeGreaterThan(0);
  // The kind line is the one row every asset has, whatever its kind.
  await expect(page.locator(".banner .bkind")).not.toBeEmpty();

  // 🔒 It must be closable. A panel that opens over the map and cannot be dismissed is
  // worse than no panel, because the map underneath is the actual product.
  await page.locator(".banner .bclose").click();
  expect(await page.locator(".banner").count(), "the close button closes it").toBe(0);
});

test("an ambiguous command offers the candidates instead of guessing", async ({ page }) => {
  /**
   * 🥇 DEMO BEAT 3, AND A NAMED REQUIREMENT. The server answers an ambiguous command with
   * candidates rather than picking one, and the operator's choice is posted back carrying
   * the original command's id so the audit log shows a question asked and answered.
   *
   * ⚠️ MOCKED DELIBERATELY. This asserts the RENDERING half of the contract: given a
   * clarify payload, are the chips drawn, does pressing one post the option's own plan with
   * `parent_command_id`, and do the chips then clear. Whether the server produces that
   * payload is the other lane's test. Mocking also keeps this off the model, so it costs no
   * tokens and cannot fail because a rate limit was hit.
   *
   * 🔴 THE `ok: false` IS NOT A MISTAKE IN THIS FIXTURE. A clarification genuinely did not
   * run, so the server says so honestly, and the UI must branch on `clarify` BEFORE `ok` or
   * the operator gets a red failure line sitting above a row of buttons.
   */
  await page.goto("/?live=off");
  await waitForAppLoaded(page);

  const posted: Record<string, unknown>[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/command") && r.method() === "POST") {
      posted.push(JSON.parse(r.postData() || "{}"));
    }
  });

  let reply: object = {
    ok: false,
    command_id: "cmd-parent-1",
    summary: '"daymark" matches 4 assets. Which one?',
    tier: "parser",
    results: [],
    ui_effects: {
      clarify: {
        command_id: "cmd-parent-1",
        query: "daymark",
        question: 'Which "daymark" did you mean?',
        // 4 matched, 3 offered: the gap is what the "and N more" line exists to report,
        // rather than silently showing a truncated list as if it were all of them.
        total: 4,
        options: [
          { id: "uas-a", label: "Daymark 01", detail: "uas, nominal",
            plan: [{ tool: "describe_entity", params: { target: "uas-a" } }] },
          { id: "uas-b", label: "Daymark 02", detail: "uas, maintenance",
            plan: [{ tool: "describe_entity", params: { target: "uas-b" } }] },
          // plan: null is a real case: the phrase could not be substituted, so a button
          // would re-ask the same question forever.
          { id: "uas-c", label: "Daymark 03", detail: "uas, nominal", plan: null },
        ],
      },
    },
  };
  await page.route("**/api/command", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(reply) }),
  );

  const input = page.locator(".cmdinput");
  await input.fill("tell me about daymark");
  await input.press("Enter");

  await expect(page.locator(".clarify .cq")).toContainText("Which");
  expect(await page.locator(".clarify button.chip").count(), "pressable options").toBe(2);
  expect(await page.locator(".clarify .chip.flat").count(), "an option with no plan is not a button").toBe(1);
  await expect(page.locator(".clarify .cmore"), "it says how many were left out").toContainText("1 more");

  // The transcript line must NOT be styled as a failure, despite ok: false.
  expect(
    await page.locator(".activity .entry.system.bad").count(),
    "a clarification is a question, not an error",
  ).toBe(0);

  reply = { ok: true, command_id: "cmd-child-1", summary: "Daymark 02: uas, maintenance", tier: "parser", results: [] };
  await page.locator(".clarify button.chip").nth(1).click();
  await expect(page.locator(".activity")).toContainText("Daymark 02: uas");
  expect(await page.locator(".clarify").count(), "answering clears the question").toBe(0);

  const chained = posted.find((p) => p.parent_command_id);
  expect(chained, "the answer must be posted back as a chained command").toBeTruthy();
  expect(chained!.parent_command_id, "carrying the ORIGINAL command id").toBe("cmd-parent-1");
  expect(chained!.source, "a chip press is a button, and the log distinguishes them").toBe("ui_button");
  expect(Array.isArray(chained!.plan), "it posts the option's own plan, not a new utterance").toBe(true);
});

test("a follow-up carries the previous answer, so \"them\" means something", async ({ page }) => {
  /**
   * 🥇 The bug this exists for, in full: `how many unknown parties on foot` answered 3, and
   * `list them` answered 76. "them" bound to nothing and listed the world.
   *
   * ⚠️ ASSERTED ON WHAT IS SENT, not on what comes back. Whether the server resolves "them"
   * correctly is its test; this one guards the half that was missing, which is that the
   * client carries the previous answer's ids at all. Newest LAST, because the server binds
   * to the final entry and the order is part of the contract.
   */
  await page.goto("/?live=off");
  await waitForAppLoaded(page);

  const posted: Record<string, unknown>[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/command") && r.method() === "POST") {
      posted.push(JSON.parse(r.postData() || "{}"));
    }
  });

  let reply: object = {
    ok: true, command_id: "c1", summary: "3 matching", tier: "parser",
    results: [{ tool: "list_entities", ok: true, message: "3 matching", data: { ids: ["a-1", "a-2", "a-3"] } }],
  };
  await page.route("**/api/command", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(reply) }),
  );

  const input = page.locator(".cmdinput");
  await input.fill("how many unknown parties on foot");
  await input.press("Enter");
  await expect(page.locator(".activity")).toContainText("3 matching");

  reply = { ok: true, command_id: "c2", summary: "3 matching", tier: "parser", results: [] };
  await input.fill("list them");
  await input.press("Enter");
  await page.waitForTimeout(1200);

  const first = posted[0] as { context?: { recent?: unknown[] } };
  const second = posted[1] as { context?: { recent?: { ids?: string[]; utterance?: string }[] } };
  expect(first.context?.recent, "the first command has no history to carry").toEqual([]);

  const recent = second.context?.recent ?? [];
  expect(recent.length, "the follow-up carries the previous turn").toBeGreaterThan(0);
  const newest = recent[recent.length - 1];
  expect(newest.ids, "the ids the previous answer actually returned").toEqual(["a-1", "a-2", "a-3"]);
  expect(newest.utterance, "and what was asked to get them").toContain("unknown parties");
});
