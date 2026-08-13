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
 * The kinds that carry one of our radios, which is to say the equipment we operate and
 * can hear from.
 *
 * 🔑 THIS IS THE LINE THE STATUS STRIP COUNTS AGAINST, and drawing it here is what makes
 * those numbers add up. Two things sit outside it, for the same reason. A CONTACT is what
 * the network is watching rather than part of it: a ship is not one of our assets that
 * happens to be unreachable. A RADAR SITE is friendly but not ours, carries `owned:
 * false`, and reports to its own operator, so it can be neither reachable nor overdue to
 * us. Every condition the strip tracks is a fact about our own kit, and neither of those
 * two can answer any of them.
 *
 * ⚠️ MIRRORS `mesh.MESH_KINDS` ON THE SERVER, WHICH IS A DRIFT RISK AND IS PINNED BY A
 * TEST. `test_the_client_and_server_agree_on_the_mesh_kinds` reads this file and compares
 * the set against the Python one, so adding a kind on one side and not the other is a
 * failing suite rather than a status strip that quietly miscounts.
 */
export const MESH_KINDS: ReadonlySet<string> = new Set<AssetKind>([
  "node",
  "patrol",
  "uas",
  "launch_site",
  "hydrophone",
]);

/**
 * ⚠️ MID-MIGRATION, AND BOTH SHAPES ARE LISTED ON PURPOSE. The server is moving to
 * `nominal` plus `maintenance`; the other three are what it still serves today. Narrowing
 * this type before the server narrows its output would not make the old values stop
 * arriving, it would only stop TypeScript from admitting they do.
 */
export type AssetStatus = "nominal" | "maintenance" | "degraded" | "warning" | "silent";

/** Exactly one of these per asset, once the server ships it. */
export type AssetFlag = "nominal" | "maintenance" | "overdue";

/**
 * Who operates this thing, derived on the server from the kind declaration.
 *
 * 🔑 SEPARATE FROM `AssetThreat` ON PURPOSE. A NORAD radar site and an unidentified vessel
 * are both "not ours" and are not remotely the same thing: one is a known fixed
 * installation somebody else runs, the other is a contact that will not say what it is.
 * A single `owned` boolean put them in one bucket.
 */
export type AssetRelationship = "ours" | "third_party" | "contact";

/** How it should be regarded. `unknown` is the honest default for an unjudged contact. */
export type AssetThreat = "friendly" | "unknown" | "hostile";

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
  /** Who operates it. Server-derived; `undefined` from a build that does not send it. */
  relationship?: AssetRelationship;
  /** How it is regarded. Server-derived, and never guessed on the client. */
  threat?: AssetThreat;
  /** How far this asset can hold a contact, effective rather than stored. Sensors only. */
  detectionRadiusKm?: number;
  /** Server-computed staleness. `undefined` until the server ships it. */
  overdue?: boolean;
  /** Contacts only: is it actually on our picture. Absent on anything that is not a contact. */
  tracked?: boolean;
  /** Contacts only: how many sensors are holding it, whether or not they can report. */
  held?: number;
  /**
   * We hold it and it will not say what it is: on our picture, identity unknown.
   *
   * 🔒 SERVER-COMPUTED, and deliberately not derived here even though the inputs are on the
   * wire. `tracked` means "announcing OR reported", so it covers a contact that is simply
   * telling us who it is, and "announcing" falls through AIS to a transponder to an
   * emitting flag. Working it out client-side gets the common case right and disagrees with
   * the server on the rest, which is the sort of drift this display exists to avoid.
   */
  detectedUnknown?: boolean;
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
  relationship?: AssetRelationship;
  threat?: AssetThreat;
  detection_radius_km?: number;
  tracked?: boolean;
  held?: number;
  detected_unknown?: boolean;
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
    relationship: a.relationship,
    threat: a.threat,
    detectionRadiusKm: a.detection_radius_km,
    tracked: a.tracked,
    held: a.held,
    detectedUnknown: a.detected_unknown,
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
  // 🔴 A CONTACT IS NEVER UNREACHABLE, AND THIS LINE IS THE WHOLE FIX. Reachability is a
  // fact about OUR network: it means no path exists from this asset back to a gateway. A
  // contact was never on that network, so the question does not apply to it, and answering
  // it anyway put a grey "unreachable" ring on two unidentified contacts.
  //
  // ⚠️ THE TWO STATES ARE NOT THE SAME AND WANT DIFFERENT ACTIONS. Ours going quiet means
  // go and look at what stopped relaying. A contact going quiet means we have lost track of
  // something we do not control, and the answer is to re-acquire it. `overdue` already says
  // that, in a word that is true for both.
  //
  // This function's own docstring used to call the fallback below temporary. It was not
  // temporary, it was wrong for a third of the map.
  if (asset.relationship === "contact") return false;
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
/**
 * 🔑 ONE STORED FIELD, AND THE UNIT IS A DISPLAY DECISION. Speed is kept as `speed_kmh`
 * everywhere and rendered in the unit its kind is actually spoken in: knots at sea and in
 * the air, km/h on the ground. This used to read three different props with three different
 * names, mirroring a three-way lookup on the server, so one quantity needed the same
 * conversion table in two places and either could miss a case.
 */
const KNOTS_KINDS = new Set<string>(["vessel", "aircraft"]);
const KM_PER_KNOT = 1.852;

export function speedOf(asset: Asset): string | null {
  const kmh = (asset.props ?? {}).speed_kmh;
  if (typeof kmh !== "number") return null;
  if (KNOTS_KINDS.has(asset.kind)) return `${Math.round(kmh / KM_PER_KNOT)} kn`;
  return `${Math.round(kmh)} km/h`;
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

/**
 * Every kind, derived from the label table rather than written out again.
 *
 * 🔑 ONE LIST, SO "show only the vessels" CANNOT GO STALE. That command hides every kind
 * except the named one, which means it needs to know what "every kind" is. A second
 * hand-maintained array would work until somebody added a kind and forgot it, and the
 * symptom would be a kind that silently refuses to hide.
 */
export const ALL_KINDS = Object.keys(KIND_LABEL) as AssetKind[];


/**
 * UNDETECTED UNKNOWN: contacts the console cannot honestly claim to have.
 *
 * 🔑 THE LINE IS "DID THE DETECTION REACH THIS CONSOLE", NOT "DOES A SENSOR HOLD IT". That
 * distinction is what puts `detected_not_reported` on this side of it rather than with the
 * contacts we have. A sensor holding something it cannot tell us about leaves the console in
 * exactly the position it would be in if nothing held it at all. Different faults, same
 * honest answer: we do not have this contact.
 *
 * 🔒 THE DEFAULT VIEW CLAIMS ONLY WHAT ACTUALLY ARRIVED, and these two buckets did not.
 * They are different problems with different answers, which is why they are one field with
 * two values rather than a single "unknown" flag:
 *
 * - **`detected_not_reported`** — a sensor IS holding it and cannot deliver the report.
 *   Counter-intuitive, and it is still excluded: if the report is not reaching you, you do
 *   not have the contact. That is a LINK fault, not a coverage gap, and the answer is to
 *   fix the relay.
 * - **`untracked`** — nothing holds it and it is not talking, so the console **cannot
 *   legitimately know it exists**. This one is read out of the seeded world rather than
 *   derived from the sensor network, which is the same class of claim as a modelled
 *   measurement.
 *
 * ⛔ NEITHER MAY EVER REACH A DEFAULT COUNT, SUMMARY LINE OR STATUS BADGE, and `untracked`
 * least of all. Revealing them has to be a deliberate act by the operator.
 */
export type UnknownState = "detected_not_reported" | "untracked" | null;

export function unknownState(asset: Asset): UnknownState {
  // `tracked` is absent on anything that is not a contact, and absent is not `false`.
  if (asset.tracked !== false) return null;
  return (asset.held ?? 0) > 0 ? "detected_not_reported" : "untracked";
}

/** Is this contact an adversary? Ordinary traffic that is simply quiet is not one. */
export function isHostile(asset: Asset): boolean {
  return asset.props?.hostile === true;
}

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
  /**
   * Assets something we hear from can carry a message home for, and those it cannot.
   *
   * 🔑 TAKEN FROM THE SERVER RATHER THAN RECOMPUTED HERE. Reachability is a property of
   * the whole graph, and the client holds no link budget, no gateway roles and no view of
   * which relays are being heard. `isUnreachable` falls back to "overdue means cut off"
   * precisely because these were being dropped on the way in, and that fallback applies a
   * mesh rule to contacts, which are not on the mesh at all.
   *
   * ⚠️ THESE TWO PARTITION THE MESH-CAPABLE SET, NOT THE WORLD. Their sum is
   * `meshCapable`, because a vessel and a radar site carry no radio of ours and neither
   * word is a fact about them.
   */
  serverReachable: string[];
  unreachable: string[];
  /**
   * Which sensor is currently holding which contact.
   *
   * 🔑 THE SECOND GRAPH OVER THE SAME ASSETS, and it answers the question the mesh graph
   * cannot: not "can this asset talk to us" but "how do we know that thing is there".
   * Computed on the server for the same reason the links are, the client holds no sensor
   * table and no ranges.
   */
  detections: Detection[];
}

export interface Detection {
  sensorId: string;
  contactId: string;
  distanceKm: number;
  /**
   * Does this detection actually reach us?
   *
   * 🔒 FALSE MEANS THE SENSOR HOLDS IT AND THE REPORT CANNOT GET HOME, which the console
   * must not draw as knowledge. It is shipped rather than filtered away because the
   * coverage view exists to reveal exactly that gap behind a control.
   */
  reported: boolean;
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
    serverReachable: (body.server_reachable ?? []) as string[],
    unreachable: (body.unreachable ?? []) as string[],
    detections: ((body.detections ?? []) as Record<string, unknown>[]).map((d) => ({
      sensorId: d.sensor_id as string,
      contactId: d.contact_id as string,
      distanceKm: d.distance_km as number,
      reported: Boolean(d.reported),
    })),
  };
}

// ---------------------------------------------------------------------------
// Sea ice
// ---------------------------------------------------------------------------

/**
 * One date's measurement grid, as measured. NOT a map shape.
 *
 * 🔒 The store holds domain objects and the renderer turns them into whatever it draws, so
 * what crosses this boundary is a grid of concentrations with the header needed to place
 * it. Building the texture is the map's job and lives in the map.
 */
export interface IceLayer {
  /** The date shown, which is one of the vendored measurements and never between two. */
  date: string;
  /** Every date available, so the control can offer only real measurements. */
  dates: string[];
  /** Arctic-wide extent on that date, in km2, computed on the SOURCE grid at build time. */
  extentKm2: number;
  caveat: string;
  citation: string;

  /** One byte per cell, `0-100` percent, `poleHoleValue` for unmeasured. Row 0 is SOUTH. */
  cells: Uint8Array;
  cols: number;
  rows: number;
  /** Lon/lat of the south-west corner of cell (0,0). */
  origin: [number, number];
  /** Degrees per cell, lon then lat. */
  step: [number, number];
  poleHoleValue: number;
}

interface IceIndex {
  origin: [number, number];
  step: [number, number];
  cols: number;
  rows: number;
  dates: string[];
  /** A template, e.g. `ice/{date}.png`. */
  tiles: string;
  extent_km2: Record<string, number>;
  poleHoleValue: number;
  source: { citation: string; caveat: string };
}

let indexPromise: Promise<IceIndex> | null = null;

/**
 * The grid header and date list, fetched once.
 *
 * 🥇 THE MEASUREMENTS THEMSELVES ARE FETCHED PER DATE, and that is the whole reason this
 * shape exists. The previous payload was every date base64'd into one JSON: 11.8 MB on
 * disk, 1.4 MB gzipped, and a visitor downloaded all fifty-five before seeing one. A
 * greyscale PNG per date is about 11 KB, so the first paint now costs one date instead of
 * five years.
 */
function iceIndex(): Promise<IceIndex> {
  if (!indexPromise) {
    indexPromise = fetch("/data/ice-index.json").then((r) => {
      if (!r.ok) throw new Error(`ice index failed to load: ${r.status}`);
      return r.json() as Promise<IceIndex>;
    });
  }
  return indexPromise;
}

/**
 * Decode one date's greyscale PNG back into one byte per cell.
 *
 * ⚠️ ROW 0 IS THE SOUTHERNMOST ROW, which is upside down relative to how images are
 * normally stored and matches the grid's own convention. Verified against the data rather
 * than assumed: on a March date the first row is almost all open water, which is 55 N, and
 * the last is solid ice, which is 89 N. Getting this backwards renders a plausible-looking
 * Arctic with the ice in the wrong hemisphere of the frame.
 *
 * The image is greyscale, so all three channels carry the same number and the red one is
 * as good as any.
 */
async function decodeTile(url: string, cols: number, rows: number): Promise<Uint8Array> {
  const img = new Image();
  img.decoding = "async";
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error(`ice tile failed to decode: ${url}`));
    img.src = url;
  });

  const canvas = document.createElement("canvas");
  canvas.width = cols;
  canvas.height = rows;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2d context available to decode the ice tile");
  ctx.drawImage(img, 0, 0);

  const rgba = ctx.getImageData(0, 0, cols, rows).data;
  const cells = new Uint8Array(cols * rows);
  for (let i = 0; i < cells.length; i++) cells[i] = rgba[i * 4];
  return cells;
}

const tileCache = new globalThis.Map<string, Promise<Uint8Array>>();

/**
 * The measurement nearest the requested date.
 *
 * ⚠️ SNAPS TO A REAL MEASUREMENT rather than interpolating between two. Interpolating would
 * invent a value nobody observed, which is exactly the property this layer exists to keep.
 * The returned `date` is the one actually shown, so the control can say so.
 */
export async function fetchIce(on: string): Promise<IceLayer> {
  const f = await iceIndex();
  const want = Date.parse(`${on}T00:00:00Z`);
  const date = f.dates.reduce(
    (best, d) =>
      Math.abs(Date.parse(`${d}T00:00:00Z`) - want) <
      Math.abs(Date.parse(`${best}T00:00:00Z`) - want)
        ? d
        : best,
    f.dates[0],
  );

  // Cached per date, because stepping back and forth through months is the normal way this
  // control gets used and a tile is immutable once published.
  let tile = tileCache.get(date);
  if (!tile) {
    tile = decodeTile(`/data/${f.tiles.replace("{date}", date)}`, f.cols, f.rows);
    tileCache.set(date, tile);
  }

  return {
    date,
    dates: f.dates,
    extentKm2: f.extent_km2[date] ?? 0,
    caveat: f.source.caveat,
    citation: f.source.citation,
    cells: await tile,
    cols: f.cols,
    rows: f.rows,
    origin: f.origin,
    step: f.step,
    poleHoleValue: f.poleHoleValue,
  };
}
