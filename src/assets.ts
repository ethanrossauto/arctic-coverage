/**
 * Asset types and the client that fetches them.
 *
 * The five kinds are genuinely different, and the type reflects that rather than
 * flattening them into a bag of optional fields: shared identity and position at the
 * top level, per-kind detail in `props`. That mirrors the database exactly, which is
 * deliberate. A client that reshapes the server's model invents a second model, and
 * then every question has two answers.
 */

/**
 * ⚠️ SEVEN SEEDED KINDS PLUS `marker`, and this list has to hold every one of them.
 * `launch_site` was missing here while six were seeded, drawn and counted, so the type
 * said one thing and the database said another. Nothing broke loudly, which is the
 * problem: `icons.ts` and the paint expressions are keyed on the string, so a kind the
 * type does not know about still renders and simply cannot be named in TypeScript.
 */
export type AssetKind =
  | "node"
  | "patrol"
  | "uas"
  | "hydrophone"
  | "vessel"
  | "radar"
  | "launch_site"
  // Air and land adversaries. Added before the seed ships them, deliberately: a kind the
  // renderer does not know about still draws, as the plain marker, so the failure mode is
  // a map that looks fine and is quietly lying about what it is showing.
  | "aircraft"
  | "ground_party"
  | "marker";

/**
 * ⚠️ MID-MIGRATION, AND BOTH SHAPES ARE LISTED ON PURPOSE. The server is moving to
 * `nominal` plus `maintenance`; the other three are what it still serves today. Narrowing
 * this type before the server narrows its output would not make the old values stop
 * arriving, it would only stop TypeScript from admitting they do.
 */
export type AssetStatus = "nominal" | "maintenance" | "degraded" | "warning" | "silent";

/** Exactly one of these per asset, once the server ships it. */
export type AssetFlag = "nominal" | "maintenance" | "overdue";

/** GeoJSON LineString, as the server stores it: lon-first. */
export interface LineGeometry {
  type: "LineString";
  coordinates: [number, number][];
}

export interface Asset {
  id: string;
  kind: AssetKind;
  name: string;
  lat: number | null;
  lon: number | null;
  altM: number | null;
  status: AssetStatus;
  geometry: LineGeometry | null;
  props: Record<string, unknown>;
  /** ISO instant, or null for kinds that never report. */
  lastHeard: string | null;
  /**
   * Something we are still hearing from leads all the way back to a gateway.
   *
   * 🔑 NOT THE SAME AS THIS ASSET BEING OVERDUE. A healthy node behind an overdue relay
   * fails this through no fault of its own, which is exactly why the server computes it
   * and the client must not. `undefined` means this build of the server does not send it,
   * which is different from `false`.
   */
  serverReachable?: boolean;
  /**
   * Has at least one neighbour close enough to talk to. Geometric, and it ignores health.
   *
   * ⚠️ FAILS INDEPENDENTLY OF `serverReachable`, and both cases are real: a site with its
   * own satellite backhaul and no neighbours is reachable but not mesh-connected, and a
   * cluster talking happily to itself with no way out is mesh-connected but not reachable.
   * They are different problems and they want different answers: one means put something
   * closer to it, the other means go and look at what stopped relaying.
   */
  meshConnected?: boolean;
  /** Server-computed staleness. `undefined` until the server ships it. */
  overdue?: boolean;
  /** The one flag, server-computed. `undefined` until the server ships it. */
  flag?: AssetFlag;
  /** Vessels only. `false` is the interesting case. */
  aisReporting: boolean | null;
  createdBy: "seed" | "user" | "llm";
}

interface WireAsset {
  id: string;
  kind: AssetKind;
  name: string;
  lat: number | null;
  lon: number | null;
  alt_m: number | null;
  status: AssetStatus;
  geometry: LineGeometry | null;
  props: Record<string, unknown>;
  last_heard: string | null;
  ais_reporting: boolean | null;
  created_by: "seed" | "user" | "llm";
  server_reachable?: boolean;
  mesh_connected?: boolean;
  overdue?: boolean;
  flag?: AssetFlag;
}

export async function fetchAssets(kind?: AssetKind): Promise<Asset[]> {
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  const res = await fetch(`/api/entities${qs}`);
  if (!res.ok) {
    throw new Error(`entities request failed: ${res.status} ${await res.text()}`);
  }
  const body = (await res.json()) as { entities: WireAsset[] };
  return body.entities.map((a) => ({
    id: a.id,
    kind: a.kind,
    name: a.name,
    lat: a.lat,
    lon: a.lon,
    altM: a.alt_m,
    status: a.status,
    geometry: a.geometry,
    props: a.props,
    lastHeard: a.last_heard,
    aisReporting: a.ais_reporting,
    createdBy: a.created_by,
    serverReachable: a.server_reachable,
    meshConnected: a.mesh_connected,
    overdue: a.overdue,
    flag: a.flag,
  }));
}

/** Minutes since this asset last reported, or null if it never reports. */
export function minutesSinceHeard(asset: Asset, nowMs: number): number | null {
  if (!asset.lastHeard) return null;
  return (nowMs - Date.parse(asset.lastHeard)) / 60_000;
}

/**
 * Is this asset overdue?
 *
 * Thresholds differ by kind because the kinds report on different rhythms, and a
 * single global threshold would either call every patrol overdue or never notice a
 * dead node. A mesh node beacons continuously; a Ranger patrol checks in when it
 * stops moving.
 */
// ⚠️ `radar` is absent on purpose, not by omission. Those sites do not report into
// the mesh at all, which is the interoperability problem stated as data. An asset
// cannot be overdue to a network it was never on, and giving it a threshold would
// make twelve sites permanently overdue and drown the four that really are.
const OVERDUE_MINUTES: Partial<Record<AssetKind, number>> = {
  node: 120,
  hydrophone: 180,
  uas: 60,
  patrol: 240,
  vessel: 60,
};

export function isOverdue(asset: Asset, nowMs: number): boolean {
  // The server's answer wins whenever it is there. Recomputing a rule the server already
  // applied is how a footer count and a typed answer end up disagreeing while both look
  // right, which has already happened once here.
  if (asset.flag !== undefined) return asset.flag === "overdue";
  if (asset.overdue !== undefined) return asset.overdue;

  const threshold = OVERDUE_MINUTES[asset.kind];
  const mins = minutesSinceHeard(asset, nowMs);
  return threshold !== undefined && mins !== null && mins > threshold;
}

// ---------------------------------------------------------------------------
// What the ring around an asset means
// ---------------------------------------------------------------------------

/**
 * How close a link may get to dropping before the asset wears a ring.
 *
 * The same 3 km the link colour ramp already uses, deliberately: a line going amber and
 * its endpoints gaining a ring should mean the same thing, or the display is telling two
 * stories about one fact.
 */
export const WEAK_MARGIN_KM = 3;

/**
 * The three conditions worth a ring. `null` is nominal and draws nothing.
 *
 * 🔑 THEY ANSWER DIFFERENT QUESTIONS, WHICH IS WHY THEY ARE ONE FIELD AND NOT THREE FLAGS.
 * `unreachable` is about whether we are hearing it, `maintenance` is about the kit, and
 * `weak` is about the radio path. An asset can be more than one; the precedence below
 * picks which one the operator is shown.
 */
export type RingState = "unreachable" | "maintenance" | "weak" | null;

/**
 * Which assets are one bad kilometre from losing their best link.
 *
 * 🔒 DERIVED FROM THE SERVER'S OWN LINK LIST, never recomputed from positions. This reads
 * the margins the server calculated and asks which asset's BEST one is thin. It builds no
 * second model of range, so it cannot drift from the mesh the way a client-side distance
 * calculation would.
 *
 * ⚠️ Best, not worst. An asset with one solid link and one marginal link is not about to
 * be cut off, and flagging it would make the ring meaningless on any well-connected node.
 */
export function weakAssetIds(mesh: MeshStatus | null): Set<string> {
  const best = new globalThis.Map<string, number>();
  for (const l of mesh?.links ?? []) {
    for (const id of [l.a, l.b]) {
      const prev = best.get(id);
      if (prev === undefined || l.marginKm > prev) best.set(id, l.marginKm);
    }
  }
  const weak = new Set<string>();
  for (const [id, margin] of best) if (margin < WEAK_MARGIN_KM) weak.add(id);
  return weak;
}

/** How many assets this one can currently talk to, counted off the server's link list. */
export function connectionCount(id: string, mesh: MeshStatus | null): number {
  return (mesh?.links ?? []).filter((l) => l.a === id || l.b === id).length;
}

/**
 * Which ring an asset wears, if any.
 *
 * **Precedence: unreachable, then maintenance, then weak**, and the order is the point.
 * Grey means we are not hearing from this asset, so everything else the display could say
 * about it is stale by definition. Announcing "needs maintenance" about a thing we lost
 * contact with an hour ago states an old fact as a current one.
 *
 * ⚠️ READS THE NEW FIELDS WHEN THEY EXIST AND BRIDGES TO THE OLD ONES WHEN THEY DO NOT.
 * The server is mid-migration from four statuses to `nominal`/`maintenance` plus a
 * computed `reachable`, and a client that assumed the new shape would blank every ring on
 * the map until that lands. The bridge is temporary and should be deleted, not kept.
 */
export function isUnreachable(asset: Asset, nowMs: number): boolean {
  // Unreachable propagates: an asset behind an overdue relay is grey even though nothing
  // is wrong with it. Only the server can know that, so there is no fallback for the
  // propagated case, only for the asset's own silence.
  if (asset.serverReachable !== undefined) return !asset.serverReachable;
  return isOverdue(asset, nowMs) || asset.status === "silent";
}

/**
 * Does this asset carry the satellite terminal its cluster relays through?
 *
 * 🔑 A GATEWAY IS A ROLE, NOT A KIND. Today it is a launch site with `mesh_gateway` set;
 * a node carrying a backhaul terminal is the same role on a different kind of box. Keying
 * on the property rather than on the kind means the second case needs no change here.
 */
export function isGateway(asset: Asset): boolean {
  // 🔴 THE FIELD CHANGED UNDER US AND THE FAILURE WAS SILENT. This read `mesh_gateway`,
  // which the reseed deleted; the seed now carries `backhaul`. Nothing errored, nothing
  // logged, every badge simply stopped drawing. That is the exact failure mode this
  // feature was asked to avoid, so both names are read and the old one stays until the
  // last database holding it is gone.
  //
  // Truthiness rather than `=== true`, because `backhaul` may name the link type rather
  // than assert a boolean, and "iridium" means it has one just as much as `true` does.
  const b = asset.props?.backhaul;
  if (b !== undefined && b !== null) {
    return b !== false && b !== "" && b !== "none";
  }
  return asset.props?.mesh_gateway === true;
}

/**
 * How fast it is going, in the units its own kind reports.
 *
 * ⚠️ NOT CONVERTED TO ONE UNIT. A vessel and an aircraft report knots, a patrol and a
 * drone report km/h, and that is how the people who operate each of them talk about
 * speed. Normalising everything to one unit would make every number slightly unfamiliar
 * to whoever actually cares about it, and would invent precision in the conversion.
 */
export function speedOf(asset: Asset): string | null {
  const p = asset.props ?? {};
  if (typeof p.speed_kn === "number") return `${p.speed_kn} kn`;
  if (typeof p.speed_kmh === "number") return `${p.speed_kmh} km/h`;
  // A drone reports the speed it cruises at rather than a measured instantaneous one, so
  // it is labelled as such instead of being passed off as the same measurement.
  if (typeof p.cruise_kmh === "number") return `${p.cruise_kmh} km/h cruise`;
  return null;
}

/** Latitude and longitude, in the form an operator would read one out. */
export function positionOf(asset: Asset): string | null {
  if (asset.lat === null || asset.lon === null) return null;
  const ns = asset.lat >= 0 ? "N" : "S";
  const ew = asset.lon >= 0 ? "E" : "W";
  return `${Math.abs(asset.lat).toFixed(3)}\u00b0${ns}  ${Math.abs(asset.lon).toFixed(3)}\u00b0${ew}`;
}

export function ringState(asset: Asset, weak: Set<string>, nowMs: number): RingState {
  if (isUnreachable(asset, nowMs)) return "unreachable";

  if (asset.status === "maintenance") return "maintenance";
  // Bridge: the two legacy statuses that meant "the kit is unwell but still reporting".
  if (asset.status === "degraded" || asset.status === "warning") return "maintenance";

  return weak.has(asset.id) ? "weak" : null;
}

/**
 * What to call each kind in front of a person. `Record<AssetKind, string>` rather than a
 * partial map on purpose: adding a kind to the type above without naming it here is then
 * a compile error rather than a blank space in the UI.
 */
export const KIND_LABEL: Record<AssetKind, string> = {
  node: "Mesh node",
  patrol: "Ranger patrol",
  uas: "UAS",
  hydrophone: "Hydrophone",
  radar: "Early-warning radar",
  vessel: "Vessel",
  launch_site: "Launch site",
  aircraft: "Aircraft",
  ground_party: "Ground party",
  marker: "Marker",
};

// ---------------------------------------------------------------------------
// The mesh link graph
// ---------------------------------------------------------------------------

/** One radio link that is currently up, as the server computed it. */
export interface MeshLink {
  a: string;
  b: string;
  distanceKm: number;
  rangeKm: number;
  /**
   * How much further apart these two could drift before the link drops.
   *
   * 🔑 This is the field worth rendering. A link with 0.6 km of margin and one with
   * 20 km are both "up", and only one of them is about to stop being up.
   */
  marginKm: number;
}

/** A set of assets that can all reach each other, directly or through neighbours. */
export interface MeshGroup {
  size: number;
  members: string[];
  label: string;
}

export interface MeshStatus {
  links: MeshLink[];
  groups: MeshGroup[];
  /** Assets on no mesh at all. Reported separately from one-member groups on purpose. */
  isolated: string[];
  meshCapable: number;
}

export async function fetchMesh(): Promise<MeshStatus> {
  const res = await fetch("/api/mesh");
  if (!res.ok) throw new Error(`mesh request failed: ${res.status} ${await res.text()}`);
  const body = await res.json();
  return {
    links: body.links.map((l: Record<string, number | string>) => ({
      a: l.a as string,
      b: l.b as string,
      distanceKm: l.distance_km as number,
      rangeKm: l.range_km as number,
      marginKm: l.margin_km as number,
    })),
    groups: body.groups as MeshGroup[],
    isolated: body.isolated as string[],
    meshCapable: body.mesh_capable as number,
  };
}

// ---------------------------------------------------------------------------
// Sea ice
// ---------------------------------------------------------------------------

export interface IceLayer {
  /** The date actually shown, which is the nearest vendored measurement. */
  date: string;
  grid: unknown;
  /** Arctic-wide ice extent on that date, in km2, computed on the source grid. */
  extentKm2: number;
  caveat: string;
  citation: string;
  /** Every date available, so the control can offer only real measurements. */
  dates: string[];
  iceCells: number;
}

/** The vendored measurement set, exactly as scripts/build_ice_history.py wrote it. */
interface IceFile {
  kind: string;
  origin: [number, number];
  step: [number, number];
  cols: number;
  rows: number;
  dates: string[];
  concentration: Record<string, string>;
  extent_km2: Record<string, number>;
  poleHoleValue: number;
  source: { citation: string; caveat: string };
}

let icePromise: Promise<IceFile> | null = null;

/**
 * The whole vendored measurement set, fetched once.
 *
 * 🔑 MEASURED, NOT MODELLED. Every value here is sea ice concentration observed by
 * satellite on that date and published by NSIDC. There is no model in this path, which is
 * the entire reason it replaced one: nothing to calibrate and nothing to defend.
 *
 * ⚠️ CONCENTRATION IS NOT THICKNESS. It is the fraction of sea surface covered by ice on a
 * 25 km grid, and it says nothing about what will bear a load. The UI shows `caveat`
 * wherever it shows the layer.
 */
function iceFile(): Promise<IceFile> {
  if (!icePromise) {
    icePromise = fetch("/data/ice.json").then((r) => {
      if (!r.ok) throw new Error(`ice measurements failed to load: ${r.status}`);
      return r.json() as Promise<IceFile>;
    });
  }
  return icePromise;
}

function decode(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * Build the drawable grid for whichever measurement is nearest the requested date.
 *
 * ⚠️ SNAPS TO A REAL MEASUREMENT rather than interpolating between two. Interpolating
 * would invent a value nobody observed, which is exactly the property this layer exists to
 * avoid. The returned `date` is the one actually shown, so the UI can say so.
 */
/**
 * Soften the shading between measured cells, WITHOUT changing the resolution.
 *
 * 🔑 THIS REPLACED AN UPSAMPLER, AND THE DIFFERENCE IS THE WHOLE POINT. The old version
 * subdivided every cell and interpolated, which looked right and cost four times the
 * polygons. That was affordable while the vendored grid was coarse. It stopped being
 * affordable the moment the build shipped 960 x 175: the same trick would have drawn
 * 672,000 cells and it froze the display for twelve seconds.
 *
 * A neighbourhood average at the SAME resolution buys the same softness for a fraction of
 * the cost, because the geometry does not change at all. Only the values do, and adjacent
 * values being closer together is exactly what makes a gradient read as a gradient rather
 * than as a grid.
 *
 * 🔒 THE ICE EDGE STAYS WHERE THE SATELLITE PUT IT. Whether a cell is drawn at all is
 * decided on its ORIGINAL value against the 15% threshold, never on the softened one. So
 * the boundary on screen is still the boundary NSIDC publishes; softening changes how the
 * inside is shaded, not where the ice is. The extent figure in the footer is computed on
 * the source grid at build time and is untouched by any of this.
 *
 * 🔒 THE POLE HOLE IS NEVER AVERAGED, and a cell whose neighbourhood touches it is left
 * exactly as measured. The hole is a sentinel meaning "the instrument cannot see here",
 * not a concentration. Blending it with a real reading would draw a gradient across the
 * one region of the map that is honestly unknown, and it has its own colour precisely so
 * that never happens.
 *
 * ⚠️ It costs polygons even so, because softened neighbours are less likely to be
 * identical and the run merge below has less to merge: 13,052 becomes 23,465 on a dense
 * date. That is the price of the gradient and it is worth paying at this size.
 */
function soften(cells: Uint8Array, cols: number, rows: number, hole: number): Uint8Array {
  const out = new Uint8Array(cells);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const i = r * cols + c;
      if (cells[i] === hole) continue;

      let sum = 0;
      let n = 0;
      let touchesHole = false;
      for (let dy = -1; dy <= 1 && !touchesHole; dy++) {
        const rr = r + dy;
        if (rr < 0 || rr >= rows) continue;
        for (let dx = -1; dx <= 1; dx++) {
          // Longitude wraps: the grid closes the circle, so the column east of the last
          // is the first. Clamping instead would leave a seam down the antimeridian.
          const cc = (((c + dx) % cols) + cols) % cols;
          const w = cells[rr * cols + cc];
          if (w === hole) {
            touchesHole = true;
            break;
          }
          // Zero is open water, a real 0% reading, so it belongs in the average: that is
          // what feathers the ice edge instead of leaving it a cliff.
          sum += w;
          n++;
        }
      }
      if (!touchesHole && n > 0) out[i] = Math.round(sum / n);
    }
  }
  return out;
}

export async function fetchIce(on: string): Promise<IceLayer> {
  const f = await iceFile();
  const want = Date.parse(`${on}T00:00:00Z`);
  const date = f.dates.reduce((best, d) =>
    Math.abs(Date.parse(`${d}T00:00:00Z`) - want) < Math.abs(Date.parse(`${best}T00:00:00Z`) - want)
      ? d
      : best,
  f.dates[0]);

  const raw = decode(f.concentration[date]);
  const shade = soften(raw, f.cols, f.rows, f.poleHoleValue);
  const [lon0, lat0] = f.origin;
  const [dlon, dlat] = f.step;
  const cols = f.cols;
  const rows = f.rows;

  // 🥇 ADJACENT CELLS OF THE SAME VALUE ARE MERGED INTO ONE RECTANGLE, and this is the
  // difference between a layer that draws and one that hangs.
  //
  // The vendored grid is 960 x 175, so a dense date is 62,030 cells above the ice-edge
  // threshold. Emitted one polygon per cell, MapLibre's worker spends about ten seconds
  // parsing and tiling them, and NOTHING ELSE DRAWS IN THE MEANTIME: the asset layer waits
  // its turn behind the ice, so the map sat empty for twelve seconds while the footer
  // already reported 76 assets. Measured on the real file.
  //
  // 🔒 IT LOSES NOTHING. Only cells carrying the IDENTICAL measured value are joined, so
  // every rectangle still states exactly what the satellite recorded for the ground it
  // covers. This is not smoothing, resampling or bucketing; it is the same picture with
  // fewer draw calls. On that date it turns 62,030 polygons into 13,052.
  //
  // Runs are horizontal only. Merging vertically as well would need a proper rectangle
  // decomposition for a fraction more, and this is already the difference between seconds
  // and milliseconds.
  const CHUNK_ROWS = 40;
  const features: object[] = [];
  const hole = f.poleHoleValue;
  const drawable = (v: number) => !(v === 0 || (v < 15 && v !== hole));

  for (let r = 0; r < rows; r++) {
    // Yield between bands so a big grid cannot freeze the page while it builds. A
    // macrotask, not a microtask: `await Promise.resolve()` stays in the same task and
    // yields to nothing, which is the version of this that looks right and does nothing.
    if (r > 0 && r % CHUNK_ROWS === 0) await new Promise((res) => setTimeout(res, 0));

    const lat = lat0 + r * dlat;
    let runStart = -1;
    let runValue = -1;

    const flush = (endCol: number) => {
      if (runStart < 0) return;
      const lon = lon0 + runStart * dlon;
      const east = lon0 + endCol * dlon;
      features.push({
        type: "Feature",
        properties: { concentration: runValue === hole ? -1 : runValue },
        geometry: {
          type: "Polygon",
          coordinates: [[
            [lon, lat], [east, lat], [east, lat + dlat], [lon, lat + dlat], [lon, lat],
          ]],
        },
      });
      runStart = -1;
    };

    for (let c = 0; c < cols; c++) {
      const i = r * cols + c;
      // 🔒 Drawn-or-not comes from the MEASUREMENT; the colour comes from the softened
      // value. Deciding both on the softened one would let the shading move the ice edge.
      if (!drawable(raw[i])) {
        flush(c);
        continue;
      }
      const v = raw[i] === hole ? hole : shade[i];
      if (runStart >= 0 && v === runValue) continue;
      flush(c);
      runStart = c;
      runValue = v;
    }
    flush(cols);
  }

  return {
    date,
    grid: { type: "FeatureCollection", features },
    extentKm2: f.extent_km2[date] ?? 0,
    caveat: f.source.caveat,
    citation: f.source.citation,
    dates: f.dates,
    iceCells: features.length,
  };
}
