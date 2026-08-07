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
 * Wait until the server's window has arrived and reached the UI.
 *
 * Keyed on the satellite count in the status strip rather than on a timeout,
 * because that number can only be non-zero once the fetch resolved, the payload
 * parsed and React rendered. It is the shortest honest proof that the whole
 * chain from Python to the DOM is intact.
 */
export async function waitForWindowLoaded(page: Page): Promise<void> {
  await page.getByText(/sats\s+\d+/).waitFor({ state: "visible", timeout: 30_000 });
}

/** The playback clock as the UI renders it, parsed back into a Date. */
export async function readClock(page: Page): Promise<Date> {
  const text = await page.locator(".clock").innerText();
  // Rendered as "2026-08-07  12:34:56Z" with two spaces where the T belongs.
  const iso = text.trim().replace(/\s+/, "T").replace(/Z$/, "Z");
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) throw new Error(`could not parse clock: ${JSON.stringify(text)}`);
  return d;
}

/**
 * How many distinct colours are visible in the map area?
 *
 * This is the assertion that catches a blank map. A style that fails to load, a
 * source that will not parse, or a worker that 404s all leave a flat canvas while
 * the surrounding UI keeps working perfectly, which is what makes that class of
 * bug so easy to miss.
 *
 * ⚠️ IT USES A PLAYWRIGHT SCREENSHOT, NOT `canvas.drawImage`, AND THAT MATTERS.
 * The obvious implementation reads the canvas back in the page with drawImage and
 * getImageData. On a WebGL canvas that returns nothing, because the drawing buffer
 * is cleared after compositing unless the context was created with
 * `preserveDrawingBuffer: true`. MapLibre leaves that off for performance, and
 * turning it on to satisfy a test would mean paying a real production cost for a
 * test convenience. A screenshot captures the composited result instead, so it
 * measures exactly what a person would see and costs the application nothing.
 *
 * Deliberately NOT a golden-image comparison. A reference screenshot of a WebGL
 * globe fails on any driver difference, needs regenerating after every visual
 * tweak, and answers "something changed" when the useful question is "is it
 * blank". Counting distinct colours answers that one question and leaves how it
 * LOOKS to a human, which is where that judgment belongs.
 */
export async function mapColourCount(page: Page): Promise<number> {
  const buf = await page.locator(".map").screenshot();
  const png = PNG.sync.read(buf);
  const seen = new Set<number>();
  const step = 4; // sample a grid; full resolution adds cost and no information
  for (let y = 0; y < png.height; y += step) {
    for (let x = 0; x < png.width; x += step) {
      const i = (png.width * y + x) << 2;
      seen.add((png.data[i] << 16) | (png.data[i + 1] << 8) | png.data[i + 2]);
    }
  }
  return seen.size;
}
