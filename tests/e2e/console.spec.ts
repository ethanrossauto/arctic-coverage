import { expect, test } from "@playwright/test";

import { collectPageProblems, waitForAppLoaded } from "./helpers";

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

  await page.goto("/");
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
  const gotIce = page.waitForResponse(
    (r) => r.url().includes("/data/ice.json") && r.status() === 200,
  );

  pageOrigin = new URL(test.info().project.use.baseURL!).origin;
  await page.goto("/");
  await gotBasemap;
  const entities = await (await gotEntities).json();
  const ice = await (await gotIce).json();

  expect(entities.entities.length, "seeded entities").toBe(68);
  expect(ice.kind, "the ice layer must be measured, never modelled").toBe("measured");
  expect(ice.dates.length, "vendored measurement dates").toBeGreaterThan(50);

  await page.waitForTimeout(1000);
  expect(offOrigin, "requests to third-party hosts").toEqual([]);
});
