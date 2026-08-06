/**
 * Interpolation and event tests.
 *
 * These exist because the two bugs they guard against are INVISIBLE in code
 * review and obvious on screen, which is the worst combination. Both come from
 * the same mistake, interpolating in lat/lon instead of on the sphere:
 *
 *   - A step across the antimeridian sweeps the satellite the long way round the
 *     planet. Polar orbits cross the antimeridian region twice per orbit, so this
 *     is not an edge case here.
 *   - A step over a pole swings longitude through a huge arc, so the dot circles
 *     the pole instead of crossing it. Every satellite in this app is near-polar.
 *
 * Run with: npm run test:js  (esbuild bundles the TS, then node --test)
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { activeLinks, nextEvent, positionAt } from "../../.build/playback.mjs";

const track = (pts) => ({
  satelliteId: "s",
  name: "S",
  samples: pts.map(([t, lat, lon, altKm = 780]) => ({ t, lat, lon, altKm })),
});

test("a step across the antimeridian passes through 180, not back across the globe", () => {
  const mid = positionAt(track([[0, 60, 179.9], [10, 60, -179.8]]), 5);
  assert.ok(Math.abs(Math.abs(mid.lon) - 180) < 0.2, `lon was ${mid.lon}`);
});

test("a step over the pole goes over it rather than around it", () => {
  const mid = positionAt(track([[0, 88, 0], [10, 88, 180]]), 5);
  assert.ok(mid.lat > 89, `lat was ${mid.lat}`);
});

test("the great-circle midpoint of two equatorial points is their average", () => {
  const mid = positionAt(track([[0, 0, 0], [10, 0, 90]]), 5);
  assert.ok(Math.abs(mid.lon - 45) < 1e-9);
  assert.ok(Math.abs(mid.lat) < 1e-9);
});

test("interpolation is linear in time, not eased per segment", () => {
  // An easing curve would put the quarter-point somewhere other than a quarter of
  // the arc, which makes every satellite pulse once per sample.
  const q = positionAt(track([[0, 0, 0], [10, 0, 90]]), 2.5);
  assert.ok(Math.abs(q.lon - 22.5) < 1e-9, `lon was ${q.lon}`);
});

test("altitude interpolates linearly, since a scalar has no seam", () => {
  const t = track([[0, 0, 0, 700], [10, 0, 1, 800]]);
  assert.ok(Math.abs(positionAt(t, 5).altKm - 750) < 1e-9);
});

test("outside the window returns null rather than a clamped position", () => {
  // A clamped position renders as a satellite frozen in space. The caller needs
  // to know to fetch the next window instead.
  const t = track([[0, 0, 0], [10, 0, 90]]);
  assert.equal(positionAt(t, -1), null);
  assert.equal(positionAt(t, 11), null);
});

const pass = (aos, los) => ({
  satelliteId: "a",
  siteId: "b",
  aos,
  los,
  tca: (aos + los) / 2,
  maxElevationDeg: 40,
  durationS: (los - aos) / 1000,
});

test("the next event is the nearest AOS or LOS strictly ahead", () => {
  const ps = [pass(1000, 5000), pass(9000, 12000)];
  assert.deepEqual(
    { at: nextEvent(ps, 0).at, kind: nextEvent(ps, 0).kind },
    { at: 1000, kind: "aos" },
  );
  // A link dropping is as operationally interesting as one coming up, so LOS
  // counts as an event.
  assert.deepEqual(
    { at: nextEvent(ps, 2000).at, kind: nextEvent(ps, 2000).kind },
    { at: 5000, kind: "los" },
  );
  assert.equal(nextEvent(ps, 99999), null);
});

test("active links come from exact interval boundaries", () => {
  const ps = [pass(1000, 5000), pass(9000, 12000)];
  assert.equal(activeLinks(ps, 3000).length, 1);
  assert.equal(activeLinks(ps, 7000).length, 0);
  assert.equal(activeLinks(ps, 1000).length, 1, "AOS itself counts as up");
  assert.equal(activeLinks(ps, 5000).length, 1, "LOS itself counts as up");
});
