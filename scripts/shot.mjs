/**
 * Capture a screenshot of the running console, for human review.
 *
 * Exists because the tests can only assert that the map is not blank; whether it
 * LOOKS right is a judgment a person has to make, and this is how that person
 * gets something to look at without starting a browser.
 *
 *   node scripts/shot.mjs [url] [outfile]
 *
 * ⚠️ It waits for actual PAINT rather than a fixed delay, using the same
 * colour-counting measure as the test suite. An earlier version waited a flat
 * seven seconds and produced a screenshot of a blank globe from a deployment the
 * suite had just confirmed was rendering, which is a good way to waste an hour
 * chasing a bug that does not exist.
 */
import { chromium } from "@playwright/test";
import { PNG } from "pngjs";

const url = process.argv[2] ?? "http://127.0.0.1:5173/";
const out = process.argv[3] ?? "/tmp/arctic-coverage.png";

function colourCount(buf) {
  const png = PNG.sync.read(buf);
  const seen = new Set();
  for (let y = 0; y < png.height; y += 4) {
    for (let x = 0; x < png.width; x += 4) {
      const i = (png.width * y + x) << 2;
      seen.add((png.data[i] << 16) | (png.data[i + 1] << 8) | png.data[i + 2]);
    }
  }
  return seen.size;
}

const browser = await chromium.launch({
  // Software WebGL has to be opted into explicitly or MapLibre never paints.
  args: ["--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader"],
});
const page = await browser.newPage({ viewport: { width: 1500, height: 940 } });
await page.goto(url, { waitUntil: "networkidle" });
// ⚠️ WAIT FOR THE ASSET COUNT, NOT FOR A LAYER THAT MAY NOT EXIST. This waited on
// `sats \d+` and went on waiting for months after the orbital layer was removed, so the
// one tool for looking at the display timed out against a display that was rendering
// perfectly. A readiness probe aimed at an optional feature fails as loudly as a broken
// app and means something entirely different.
// Colon optional: a readiness probe must not break because a label gained punctuation.
await page.getByText(/assets:?\s+\d+/).waitFor({ timeout: 30_000 });

let colours = 0;
const deadline = Date.now() + 45_000;
while (Date.now() < deadline) {
  colours = colourCount(await page.locator(".map").screenshot());
  if (colours > 20) break;
  await page.waitForTimeout(1000);
}
// Then let the constellation move far enough that a link is likely to be up.
await page.waitForTimeout(6000);
await page.screenshot({ path: out });
await browser.close();
console.log(`${url} -> ${out}  (${colours} distinct colours in the map area)`);
