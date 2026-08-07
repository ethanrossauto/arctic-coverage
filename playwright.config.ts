import { defineConfig, devices } from "@playwright/test";

/**
 * Browser tests for the console.
 *
 * ⚠️ THIS REVERSES A DECISION IN THE PLAN, AND THE REASON MATTERS. The plan's
 * testing section says browser and map-rendering tests are not worth it in a
 * 72-hour build, and for the usual meaning of "browser tests" that is still
 * right: nobody here is going to snapshot a React component or assert a CSS
 * colour.
 *
 * What changed is that a whole class of failure in THIS app is invisible to every
 * other kind of test. Setting `glyphs: ""` in the map style aborted MapLibre's
 * entire style load and left a blank canvas. The Python suite was green, the
 * TypeScript compiled, the interpolation tests passed, the deploy succeeded, and
 * the API returned correct data. The app was broken and nothing said so. That is
 * not "testing CSS", that is "does the page work at all", and it needs a real
 * browser to answer.
 *
 * So the suite is deliberately narrow. It asserts that the page loads without
 * console errors, that the canvas actually paints, and that server data reaches
 * the UI. It does not assert what anything looks like.
 *
 * Both servers are started automatically, so `npx playwright test` is the whole
 * command. The Python API is a real dependency of every test here: this suite
 * exercises the seam between the two, which is exactly where the deploy bug lived.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false, // one browser, one app instance, shared clock
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],

  use: {
    // Override to run the same suite against a deployed build:
    //   PLAYWRIGHT_BASE_URL=https://coverage.skryer.ca npx playwright test
    // This matters because optimizeDeps only affects the dev server, so dev
    // passing says nothing about the bundle that actually ships.
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // Generous: a cold window request propagates three satellites over five
    // sites before the first paint.
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        launchOptions: {
          // MapLibre needs a working WebGL context. Headless Chromium falls back
          // to software rendering, and modern Chrome refuses that by default, so
          // it has to be opted into explicitly. Without these flags the canvas
          // never paints and every rendering assertion here fails for a reason
          // that has nothing to do with the application.
          args: [
            "--enable-unsafe-swiftshader",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
  ],

  // Skipped when pointing at a deployed URL: there is nothing local to start.
  webServer: process.env.PLAYWRIGHT_BASE_URL ? undefined : [
    {
      command: ".venv/bin/uvicorn api.index:app --port 8000 --log-level warning",
      url: "http://127.0.0.1:8000/api/healthz",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5173",
      url: "http://127.0.0.1:5173/",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
