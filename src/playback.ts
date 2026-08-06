/**
 * Playback: turning a window of server-computed samples into smooth motion.
 *
 * The server sends satellite positions every 10 seconds and pass boundaries as
 * exact intervals. Everything in this file is PRESENTATION: it decides where to
 * draw a dot between two given points, and whether a clock falls inside a given
 * interval. No orbital mechanics happens here, and none should. That line is the
 * architecture's main rule and this is the file most likely to erode it.
 *
 * ⚠️ TWO NON-OBVIOUS DECISIONS, both of which produce visible bugs if reversed.
 *
 * 1. INTERPOLATE ON THE SPHERE, NOT IN LAT/LON. A naive lerp between two
 *    lat/lon pairs breaks in two places that this application lives in:
 *
 *      - The antimeridian. A polar orbit crosses it twice per orbit, and a
 *        sample pair straddling it (179.9E -> 179.8W) lerps the long way round,
 *        so the satellite sprints backwards across the whole planet in ten
 *        seconds of playback.
 *      - The poles. Near a pole crossing, lat/lon interpolation swings longitude
 *        through a huge arc, so the dot circles the pole instead of going over
 *        it. Every satellite here is in a near-polar orbit, so this is the
 *        normal case, not an edge case.
 *
 *    Converting both endpoints to 3D unit vectors and slerping between them has
 *    neither problem, because on a sphere there is no seam to cross.
 *
 * 2. LINEAR, NOT EASED. Applying an easing curve per segment makes every
 *    satellite accelerate and decelerate once per sample, so the whole
 *    constellation visibly pulses six times a minute. Real orbital motion is
 *    near-constant over ten seconds; linear is both simpler and correct.
 */

export interface Sample {
  /** Seconds after the window start. */
  t: number;
  lat: number;
  lon: number;
  altKm: number;
}

export interface Track {
  satelliteId: string;
  name: string;
  samples: Sample[];
}

export interface PassInterval {
  satelliteId: string;
  siteId: string;
  aos: number; // ms since epoch
  los: number;
  tca: number;
  maxElevationDeg: number;
  durationS: number;
}

export interface Position {
  lat: number;
  lon: number;
  altKm: number;
}

const DEG = Math.PI / 180;

function toUnitVector(latDeg: number, lonDeg: number): [number, number, number] {
  const lat = latDeg * DEG;
  const lon = lonDeg * DEG;
  const c = Math.cos(lat);
  return [c * Math.cos(lon), c * Math.sin(lon), Math.sin(lat)];
}

function toLatLon(v: [number, number, number]): { lat: number; lon: number } {
  const [x, y, z] = v;
  const hyp = Math.hypot(x, y);
  return {
    lat: Math.atan2(z, hyp) / DEG,
    lon: Math.atan2(y, x) / DEG,
  };
}

/**
 * Spherical linear interpolation between two surface points.
 *
 * Falls back to the first point when the two are numerically coincident, which
 * happens when a window is requested with a sample step of zero length, and
 * would otherwise divide by sin(0).
 */
function slerp(
  a: [number, number, number],
  b: [number, number, number],
  f: number,
): [number, number, number] {
  let dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  dot = Math.max(-1, Math.min(1, dot));
  const omega = Math.acos(dot);
  if (omega < 1e-9) return a;
  const s = Math.sin(omega);
  const wa = Math.sin((1 - f) * omega) / s;
  const wb = Math.sin(f * omega) / s;
  return [a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb, a[2] * wa + b[2] * wb];
}

/**
 * Where a satellite is at `elapsedS` into the window.
 *
 * Returns null outside the window rather than clamping, because a clamped
 * position is a lie that renders as a satellite frozen in space, and the caller
 * needs to know to fetch the next window instead.
 */
export function positionAt(track: Track, elapsedS: number): Position | null {
  const s = track.samples;
  if (s.length === 0) return null;
  if (elapsedS < s[0].t || elapsedS > s[s.length - 1].t) return null;

  // Samples are evenly spaced, so index directly rather than searching.
  const step = s.length > 1 ? s[1].t - s[0].t : 1;
  let i = Math.floor((elapsedS - s[0].t) / step);
  i = Math.max(0, Math.min(s.length - 2, i));
  const a = s[i];
  const b = s[i + 1];

  const span = b.t - a.t;
  const f = span > 0 ? (elapsedS - a.t) / span : 0;

  const { lat, lon } = toLatLon(
    slerp(toUnitVector(a.lat, a.lon), toUnitVector(b.lat, b.lon), f),
  );
  // Altitude is a scalar with no seam, so plain linear is right here.
  return { lat, lon, altKm: a.altKm + (b.altKm - a.altKm) * f };
}

/**
 * Which links are up at a given instant.
 *
 * Exact, because the server sent interval boundaries rather than samples. This
 * is why fast-forwarding to a precise AOS shows the link come up on that exact
 * frame instead of up to a sample-step later.
 */
export function activeLinks(passes: PassInterval[], atMs: number): PassInterval[] {
  return passes.filter((p) => atMs >= p.aos && atMs <= p.los);
}

/**
 * The next AOS or LOS strictly after `atMs`, or null if the window holds none.
 *
 * This is the whole implementation of "fast forward to the next satellite
 * event": the client already has the future, so the answer is a scan of an array
 * rather than a round trip. Both edges count as events, because a link dropping
 * is as operationally interesting as one coming up.
 */
export function nextEvent(
  passes: PassInterval[],
  atMs: number,
): { at: number; kind: "aos" | "los"; pass: PassInterval } | null {
  let best: { at: number; kind: "aos" | "los"; pass: PassInterval } | null = null;
  for (const p of passes) {
    for (const [at, kind] of [
      [p.aos, "aos"],
      [p.los, "los"],
    ] as const) {
      if (at > atMs && (best === null || at < best.at)) best = { at, kind, pass: p };
    }
  }
  return best;
}
