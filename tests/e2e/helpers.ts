import { PNG } from "pngjs";

import type { Page } from "@playwright/test";

export interface PageProblems {
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
}

/**
 * Attach listeners for everything that indicates the page is broken.
 *
 * Call this BEFORE `page.goto`, or the listeners miss everything that happens
 * during initial load, which is where the interesting failures are.
 *
 * Vite's dev client and Chrome's own software-WebGL deprecation notice are
 * filtered, because neither says anything about the application and both would
 * otherwise make the console assertion useless. Nothing else is filtered: an
 * ignore list that grows is an ignore list that stops catching things.
 */
export function collectPageProblems(page: Page): PageProblems {
  const problems: PageProblems = {
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
  };

  const ignorable = (text: string) =>
    text.includes("[vite]") ||
    text.includes("Automatic fallback to software WebGL") ||
    text.includes("GPU stall due to ReadPixels") ||
    text.includes("Download the React DevTools");

  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (!ignorable(text)) problems.consoleErrors.push(text);
  });

  page.on("pageerror", (err) => problems.pageErrors.push(err.message));

  page.on("response", (res) => {
    if (res.status() >= 400) problems.failedRequests.push(`${res.status()} ${res.url()}`);
  });

  return problems;
}

/**
 * Wait until the world has arrived from the server and reached the UI.
 *
 * Keyed on the asset count in the status strip rather than on a timeout, because
 * that number can only be non-zero once the fetch resolved, the payload parsed and
 * React rendered. It is the shortest honest proof that the whole chain from Python
 * through Postgres to the DOM is intact.
 */
export async function waitForAppLoaded(page: Page): Promise<void> {
  await page.getByText(/assets\s+\d+/).waitFor({ state: "visible", timeout: 30_000 });
}

/**
 * Wait until the vendored ice measurements have been decoded and drawn.
 *
 * Separate from the asset wait on purpose: the two load independently, and a test
 * about the ice must not pass merely because the assets arrived.
 */
export async function waitForIceLoaded(page: Page): Promise<void> {
  await page.locator(".timebar .icedate").waitFor({ state: "visible", timeout: 30_000 });
}

/**
 * How much of the map area is painted with the LAND fill colour?
 *
 * 🔴 THIS ASSERTION USED TO BE "more than three distinct colours", AND THAT WAS
 * TOO WEAK TO BE WORTH HAVING. A production build with a broken geometry worker
 * rendered no land, no coastlines and no graticule, yet still scored 191 distinct
 * colours from subtle gradient and antialiasing noise, so it sailed past a
 * threshold meant to catch a flat canvas. The suite reported the live site healthy
 * while it was blank.
 *
 * Counting pixels of a colour the map can only produce by actually drawing
 * something is a real assertion rather than a proxy for one. If land renders, the
 * source loaded, the worker parsed it and the GPU drew it. If it does not, this is
 * zero, whatever noise is on the canvas.
 *
 * Uses a Playwright screenshot rather than reading the canvas back in the page:
 * `drawImage` on a WebGL canvas returns nothing unless the context was created
 * with `preserveDrawingBuffer`, and turning that on would mean paying a real
 * production cost for test convenience.
 *
 * Still NOT a golden-image comparison. A reference image of a WebGL globe fails on
 * driver differences and answers "something changed" when the question is "did it
 * draw". What it LOOKS like stays a human's call.
 */
export async function landPixelFraction(page: Page): Promise<number> {
  const buf = await page.locator(".map").screenshot();
  const png = PNG.sync.read(buf);
  // #111b26, the land fill in GlobeMap.tsx. Tolerance absorbs the globe's subtle
  // shading without admitting the ocean (#050a10) or the graticule (#16242f).
  const [tr, tg, tb] = [0x11, 0x1b, 0x26];
  let hits = 0;
  let total = 0;
  for (let y = 0; y < png.height; y += 3) {
    for (let x = 0; x < png.width; x += 3) {
      const i = (png.width * y + x) << 2;
      total++;
      if (
        Math.abs(png.data[i] - tr) <= 6 &&
        Math.abs(png.data[i + 1] - tg) <= 6 &&
        Math.abs(png.data[i + 2] - tb) <= 6
      ) {
        hits++;
      }
    }
  }
  return total === 0 ? 0 : hits / total;
}
