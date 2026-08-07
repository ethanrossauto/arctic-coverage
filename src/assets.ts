/**
 * Asset types and the client that fetches them.
 *
 * The five kinds are genuinely different, and the type reflects that rather than
 * flattening them into a bag of optional fields: shared identity and position at the
 * top level, per-kind detail in `props`. That mirrors the database exactly, which is
 * deliberate. A client that reshapes the server's model invents a second model, and
 * then every question has two answers.
 */

export type AssetKind = "node" | "patrol" | "uas" | "hydrophone" | "vessel" | "radar" | "marker";

export type AssetStatus = "nominal" | "degraded" | "warning" | "silent";

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
  const threshold = OVERDUE_MINUTES[asset.kind];
  const mins = minutesSinceHeard(asset, nowMs);
  return threshold !== undefined && mins !== null && mins > threshold;
}

export const KIND_LABEL: Record<AssetKind, string> = {
  node: "Mesh node",
  patrol: "Ranger patrol",
  uas: "UAS",
  hydrophone: "Hydrophone",
  radar: "Early-warning radar",
  vessel: "Vessel",
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
export async function fetchIce(on: string): Promise<IceLayer> {
  const f = await iceFile();
  const want = Date.parse(`${on}T00:00:00Z`);
  const date = f.dates.reduce((best, d) =>
    Math.abs(Date.parse(`${d}T00:00:00Z`) - want) < Math.abs(Date.parse(`${best}T00:00:00Z`) - want)
      ? d
      : best,
  f.dates[0]);

  const cells = decode(f.concentration[date]);
  const [lon0, lat0] = f.origin;
  const [dlon, dlat] = f.step;

  const features = [];
  for (let i = 0; i < cells.length; i++) {
    const v = cells[i];
    // 0 is open water or land, and below 15% is the standard threshold for "not ice".
    // 255 is the satellite's pole hole: genuinely unmeasured, not ice-free.
    if (v === 0 || (v < 15 && v !== f.poleHoleValue)) continue;
    const lon = lon0 + (i % f.cols) * dlon;
    const lat = lat0 + Math.floor(i / f.cols) * dlat;
    features.push({
      type: "Feature",
      properties: { concentration: v === f.poleHoleValue ? -1 : v },
      geometry: {
        type: "Polygon",
        coordinates: [[
          [lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat], [lon, lat + dlat], [lon, lat],
        ]],
      },
    });
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
