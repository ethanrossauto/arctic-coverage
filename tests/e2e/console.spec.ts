import { expect, test } from "@playwright/test";

import { collectPageProblems, openConsole, waitForAppLoaded } from "./helpers";

/**
 * The cheapest high-value test in the repo.
 *
 * MapLibre reports most misconfiguration by emitting an error and quietly
 * refusing to render, rather than by throwing. A bad style URL, an invalid layer
 * paint property, a source that will not parse, a glyphs value that fails
 * validation: all of them produce a blank map and a console error, and nothing
 * else anywhere notices.
 *
 * That is exactly how `glyphs: ""` cost an evening. This file is the guard.
 */
test("the page loads with no console errors and no failed requests", async ({ page }) => {
  const problems = collectPageProblems(page);

  await openConsole(page, "/");
  await waitForAppLoaded(page);

  // Give MapLibre a beat past first paint: style validation errors surface during
  // source and layer setup, which happens after the DOM is ready.
  await page.waitForTimeout(1500);

  expect(problems.consoleErrors, "console errors").toEqual([]);
  expect(problems.pageErrors, "uncaught exceptions").toEqual([]);
  expect(problems.failedRequests, "failed network requests").toEqual([]);
});

test("the basemap and the world both load, and nothing is fetched off-origin", async ({
  page,
}) => {
  /**
   * The no-external-fetch property is a design decision, not an accident: the
   * tactical basemap exists so that no third party can break a demo. A decision
   * like that decays the moment someone adds a convenient CDN font or a tile
   * layer, so it is asserted rather than documented.
   */
  const offOrigin: string[] = [];
  let pageOrigin: string | null = null;
  page.on("request", (req) => {
    // Compared by ORIGIN rather than by hostname, so the same assertion holds
    // when the suite is pointed at the deployed URL with PLAYWRIGHT_BASE_URL.
    if (pageOrigin && new URL(req.url()).origin !== pageOrigin) offOrigin.push(req.url());
  });

  const gotBasemap = page.waitForResponse(
    (r) => r.url().includes("/data/land.json") && r.status() === 200,
  );
  const gotEntities = page.waitForResponse(
    (r) => r.url().includes("/api/entities") && r.status() === 200,
  );
  // The ice is a vendored file rather than an endpoint, and that is the point: the
  // measurements ship with the app, so no service can be down for them.
  //
  // ⚠️ Matches the INDEX, not a date. The payload is now one small header file plus a PNG
  // per date fetched on demand, so waiting on a particular date's image would be waiting on
  // whichever one the app happens to open with.
  const gotIce = page.waitForResponse(
    (r) => r.url().includes("/data/ice-index.json") && r.status() === 200,
  );

  pageOrigin = new URL(test.info().project.use.baseURL!).origin;
  await openConsole(page, "/");
  await gotBasemap;
  const entities = await (await gotEntities).json();
  const ice = await (await gotIce).json();

  // ⚠️ NOT A HARDCODED COUNT. This said `toBe(68)` and went red the moment the world grew
  // to 76, which is the same rot the schema comment and the module docstring both carried:
  // a number written down by hand does not move when a kind is added.
  //
  // What the test is actually for is catching a truncated or partial load, and the payload
  // can answer that about itself. A floor catches an empty world without encoding a figure
  // that changes every time the seed does.
  expect(entities.entities.length, "the payload must agree with its own count").toBe(
    entities.count,
  );
  expect(entities.entities.length, "a seeded world").toBeGreaterThan(50);
  expect(ice.kind, "the ice layer must be measured, never modelled").toBe("measured");
  expect(ice.dates.length, "vendored measurement dates").toBeGreaterThan(50);

  await page.waitForTimeout(1000);
  expect(offOrigin, "requests to third-party hosts").toEqual([]);
});

/**
 * ACCESSIBILITY.
 *
 * 🔑 THIS IS NOT BOX-TICKING FOR THIS PARTICULAR APP, for two reasons worth stating.
 *
 * The console signals state through COLOUR: grey for an asset nothing can hear, amber for
 * maintenance, a distinct treatment for a contact running dark. Colour as the sole channel
 * for information is the most common serious WCAG failure there is, and this display leans
 * on it harder than most.
 *
 * And the buyer for a system like this is a government or defence organisation, where
 * accessibility is a procurement requirement rather than a preference: AODA in Ontario,
 * WCAG in federal contracting. "It passes axe" is a commercially relevant sentence.
 *
 * ⚠️ WHAT AN AUTOMATED PASS DOES AND DOES NOT MEAN. axe catches roughly a third to a half
 * of real WCAG issues: contrast, missing labels, bad roles, unlabelled controls. It cannot
 * tell whether the map is usable by keyboard alone or whether a screen reader can follow a
 * command's result. A green run here is a floor, not a certificate, and the README says so
 * in those words rather than claiming compliance.
 */
test("the console has no serious or critical accessibility violations", async ({ page }) => {
  const { default: AxeBuilder } = await import("@axe-core/playwright");

  await openConsole(page, "/");
  await waitForAppLoaded(page);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    // ⚠️ EXCLUDED, AND NAMED RATHER THAN SILENTLY SCOPED OUT. MapLibre renders into a
    // canvas it owns and injects its own controls, so findings inside it are reports about
    // a dependency that this project cannot fix in this repo. Everything the project
    // actually wrote (the command bar, the panels, the timebar, the banner) is in scope.
    .exclude(".maplibregl-map")
    .analyze();

  // Serious and critical only. Axe's "minor" tier is largely advisory, and failing a build
  // on advice is how a check gets switched off.
  const blocking = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );

  // The failure message has to say WHAT and WHERE, or whoever sees it red at 2am learns
  // nothing from it.
  const detail = blocking
    .map((v) => `${v.id} (${v.impact}): ${v.help}\n    ${v.nodes.map((n) => n.target).join("\n    ")}`)
    .join("\n\n");

  expect(blocking, `accessibility violations:\n\n${detail}`).toHaveLength(0);
});

/**
 * 🔴 THE ENTRY SCREEN IS NOW THE ONLY WAY IN, so it gets its own test rather than being
 * exercised only as a click on the way to something else.
 *
 * What it pins is the thing the feature exists for: **nothing queries the database until a
 * visitor asks.** That is what lets the compute suspend, and suspending is what stops the
 * monthly allowance running out. A regression here is invisible on screen and shows up weeks
 * later as a bill, which is exactly the kind of thing a test has to hold.
 */
test("the entry screen asks nothing of the database until Enter is pressed", async ({ page }) => {
  // Every request the page makes, in order, so the assertion can be about absence.
  const api: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/")) api.push(u.pathname);
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Enter" }).waitFor({ state: "visible" });
  // A beat, so a poll that was going to fire has had time to fire and be caught.
  await page.waitForTimeout(1500);

  // ⚠️ THE WAKE IS EXPECTED AND IS THE POINT. `healthz` runs a real query deliberately: it is
  // what starts the compute coming up while the visitor reads the screen. What must NOT be
  // here is the console's own data, because that is the request whose latency a person would
  // actually be sitting through.
  expect(api.filter((p) => p !== "/api/healthz"), "no console data before Enter").toEqual([]);

  await page.getByRole("button", { name: "Enter" }).click();
  await waitForAppLoaded(page);

  expect(api, "entities are fetched once Enter is pressed").toContain("/api/entities");
});
