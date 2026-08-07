/**
 * The viewport bounding box contract, and why it is not just `map.getBounds()`.
 *
 * "Show me assets in the current zoom window" is one of the commands this app
 * supports, and it is unanswerable unless the client tells the server what it is
 * currently looking at. That makes this small file part of the command pipeline,
 * not part of the map.
 *
 * ⚠️ WHY A GLOBE MAKES THIS HARDER THAN IT LOOKS. On a flat mercator map the
 * visible region is a rectangle in lat/lon and `getBounds()` returns it exactly.
 * On a globe it is a curved disc, and MapLibre's `getBounds()` under globe
 * projection is an explicit approximation from a handful of test points. Two
 * consequences drive the design here:
 *
 *   1. WHEN A POLE IS VISIBLE, MapLibre deliberately returns latitude out to 90
 *      and longitude spanning the full [-180, 180]. For this application that is
 *      the NORMAL case, not a corner case: the default camera looks straight down
 *      at the Arctic. So "the current zoom window" routinely means "every
 *      longitude", and any filter that assumes otherwise silently drops assets.
 *
 *   2. AN OBLIQUE VIEW CAN PRODUCE west > east, because the visible span crosses
 *      the antimeridian. A point-in-box test written the obvious way
 *      (`lon >= west && lon <= east`) returns nothing at all in that case.
 *
 * So the wire format is a plain lat/lon box that EXPLICITLY PERMITS WRAP, and the
 * server implements containment with wraparound. Both are renderer-agnostic: the
 * globe, a mercator toggle, or a future renderer each produce this same shape,
 * which is what keeps the command layer from knowing anything about the map.
 */
import type { LngLatBounds, Map as MapLibreMap } from "maplibre-gl";

export interface ViewportBbox {
  west: number;
  south: number;
  east: number;
  north: number;
  /**
   * True when `west > east`, i.e. the box crosses the antimeridian and
   * containment must be tested as two spans. Sent explicitly rather than left for
   * the server to infer, so the intent is on the wire and cannot be
   * re-derived wrongly at the other end.
   */
  wraps: boolean;
  /**
   * True when the box spans all longitudes, which is what a pole-centred globe
   * view produces. Distinguishes "everything" from "a coincidentally wide box",
   * which matters for how a command is worded back to the user.
   */
  global: boolean;
}

const FULL_LON_EPSILON = 0.5; // degrees; MapLibre returns exactly +/-180 for a polar view

export function viewportBbox(map: MapLibreMap): ViewportBbox {
  const b: LngLatBounds = map.getBounds();
  const west = b.getWest();
  const east = b.getEast();
  const south = Math.max(-90, b.getSouth());
  const north = Math.min(90, b.getNorth());

  const spansAllLon = east - west >= 360 - FULL_LON_EPSILON;
  const wraps = !spansAllLon && west > east;

  return {
    west: normalizeLon(west),
    south,
    east: normalizeLon(east),
    north,
    wraps,
    global: spansAllLon,
  };
}

function normalizeLon(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/**
 * Client-side containment.
 *
 * ⚠️ THE SERVER TWIN DOES NOT EXIST YET. This comment previously claimed the two were
 * "kept identical in behaviour", which was a description of the intended design
 * written before the backend half was built, and it would not have survived a reader
 * who grepped for the counterpart. Stated as a gap rather than left as a claim.
 *
 * The intent stands and the duplication is deliberate when it arrives: the frontend
 * needs containment to decide what to draw, the backend needs it to answer a command,
 * and the two must agree or a filter highlights a different set than it reports. One
 * short function in each language, with this note on both sides, beats a shared module
 * for eight lines of arithmetic. **The wraparound and all-longitudes cases are the
 * part that must match**, since those are the ones a naive implementation gets wrong.
 */
export function bboxContains(bbox: ViewportBbox, lat: number, lon: number): boolean {
  if (lat < bbox.south || lat > bbox.north) return false;
  if (bbox.global) return true;
  const l = normalizeLon(lon);
  return bbox.wraps
    ? l >= bbox.west || l <= bbox.east
    : l >= bbox.west && l <= bbox.east;
}
