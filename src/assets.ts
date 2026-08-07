/**
 * Asset types and the client that fetches them.
 *
 * The five kinds are genuinely different, and the type reflects that rather than
 * flattening them into a bag of optional fields: shared identity and position at the
 * top level, per-kind detail in `props`. That mirrors the database exactly, which is
 * deliberate. A client that reshapes the server's model invents a second model, and
 * then every question has two answers.
 */

export type AssetKind = "node" | "patrol" | "uas" | "hydrophone" | "vessel" | "marker";

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
  vessel: "Vessel",
  marker: "Marker",
};
