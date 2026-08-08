/**
 * Turn a measured concentration grid into a texture MapLibre can draw.
 *
 * 🔴 THE WHOLE FILE EXISTS BECAUSE OF ONE FACT: MAPLIBRE MAPS AN IMAGE ACROSS ITS CORNERS
 * IN MERCATOR SPACE, AND THIS GRID IS LINEAR IN LATITUDE.
 *
 * Hand it the grid as it is stored and it renders a plausible-looking Arctic that is
 * simply wrong: the ice crushes into a blob near the pole and the Northwest Passage comes
 * up bare. **It throws no error.** It was verified by rendering it, not by reading docs,
 * and it is the kind of failure that survives review because the output looks like a map.
 *
 * So every output row is placed at a uniform step in Mercator y, and the source row is
 * looked up by converting that y back to a latitude. After the warp, the four corners and
 * the pixels agree, and `raster-resampling: linear` does the rest.
 *
 * 🔒 WHY A TEXTURE AT ALL, rather than the fill layer this replaced. A fill draws one
 * polygon per cell and polygons have hard edges however small the cell, so the grid is
 * always visible: that was the pixelation. More resolution could never fix it, and the
 * attempt to fix it that way is what pushed the payload to 11.8 MB. A texture is one
 * upload the GPU interpolates for free.
 */
import type { IceLayer } from "../assets";

/**
 * The concentration ramp, and the one place it lives.
 *
 * MapLibre 6.2 has no `raster-color`, so the palette cannot live in the style and has to
 * be baked into the pixels here. Checked against the shipped bundle rather than assumed:
 * the raster paint properties are brightness, contrast, hue-rotate, saturation, opacity,
 * resampling and fade-duration, and that is the whole list.
 *
 * ⚠️ Starts at 15% because that is the published threshold for the ice edge in every
 * extent figure. Below it, cells are transparent rather than faint, so the edge on screen
 * is the same edge NSIDC counts.
 */
const RAMP: [number, number, number, number][] = [
  [15, 0x4a, 0x60, 0x70],
  [60, 0x7b, 0xa8, 0xbf],
  [95, 0xcf, 0xe8, 0xf5],
];

/** The satellite cannot see the pole. Its own colour, because unmeasured is not ice-free. */
const POLE_HOLE: [number, number, number, number] = [0x3a, 0x3f, 0x52, 0.18 * 255];

/** Opacity climbs with concentration, so thicker cover reads as more present. */
const ALPHA_MIN = 0.12;
const ALPHA_MAX = 0.34;

/**
 * How many rows the warped texture gets.
 *
 * Chosen from the stretch, not from taste. Mercator expands latitude by 1/cos(lat), so the
 * southern end of this grid is its densest in y: preserving a 0.25 degree cell at 55 N
 * needs about 470 rows across the whole span. 512 keeps that with room to spare, and the
 * oversampling toward the pole costs nothing because the GPU is interpolating anyway.
 */
const TEXTURE_ROWS = 512;

/** Mercator y for a latitude in degrees. */
function mercatorY(lat: number): number {
  return Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
}

/** Latitude in degrees for a Mercator y. */
function latitudeOf(y: number): number {
  return ((2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180) / Math.PI;
}

function rampColour(v: number): [number, number, number] {
  if (v <= RAMP[0][0]) return [RAMP[0][1], RAMP[0][2], RAMP[0][3]];
  if (v >= RAMP[2][0]) return [RAMP[2][1], RAMP[2][2], RAMP[2][3]];
  const i = v < RAMP[1][0] ? 0 : 1;
  const a = RAMP[i];
  const b = RAMP[i + 1];
  const t = (v - a[0]) / (b[0] - a[0]);
  return [
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
    Math.round(a[3] + (b[3] - a[3]) * t),
  ];
}

export interface IceTexture {
  /** A data URL ready for `ImageSource.updateImage`. */
  url: string;
  /** Corner coordinates, clockwise from the north-west, as MapLibre wants them. */
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
}

/**
 * Build the texture and the corners it belongs at.
 *
 * ⚠️ THE NORTHERN EDGE IS CAPPED BELOW 90. An image source whose corner reaches the pole
 * fails outright: MapLibre resolves it to an out-of-range tile and the layer does not
 * render at all, with `y=-3 outside of bounds` on the console. Measured: 90, 89.9, 89.8
 * and 89.5 all fail, 89 renders. The vendored grid already stops at 89 for that reason,
 * and this clamp is the belt to that braces.
 *
 * 🔒 The lost sliver sits inside the pole hole, a region the display already declares as
 * unmeasured and paints in its own colour, so nothing truthful is hidden by it.
 */
export function buildIceTexture(ice: IceLayer): IceTexture {
  const [lon0, lat0] = ice.origin;
  const [dlon, dlat] = ice.step;
  const west = lon0;
  const east = lon0 + ice.cols * dlon;
  const south = lat0;
  const north = Math.min(89, lat0 + ice.rows * dlat);

  const yNorth = mercatorY(north);
  const ySouth = mercatorY(south);

  const canvas = document.createElement("canvas");
  canvas.width = ice.cols;
  canvas.height = TEXTURE_ROWS;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2d context available to build the ice texture");

  const out = ctx.createImageData(ice.cols, TEXTURE_ROWS);

  for (let j = 0; j < TEXTURE_ROWS; j++) {
    // Row 0 of an image is its TOP, which here is the northern edge.
    const y = yNorth + ((ySouth - yNorth) * j) / (TEXTURE_ROWS - 1);
    const lat = latitudeOf(y);
    // ⚠️ Row 0 of the GRID is its SOUTHERN edge, which is the opposite convention. Verified
    // against the data: on a March date the grid's first row is almost all open water.
    const srcRow = Math.round((lat - lat0) / dlat);

    for (let c = 0; c < ice.cols; c++) {
      const o = (j * ice.cols + c) << 2;
      if (srcRow < 0 || srcRow >= ice.rows) {
        out.data[o + 3] = 0;
        continue;
      }
      const v = ice.cells[srcRow * ice.cols + c];

      if (v === ice.poleHoleValue) {
        out.data[o] = POLE_HOLE[0];
        out.data[o + 1] = POLE_HOLE[1];
        out.data[o + 2] = POLE_HOLE[2];
        out.data[o + 3] = POLE_HOLE[3];
        continue;
      }
      // 0 is open water or land, and below 15% is the published "not ice" threshold.
      if (v === 0 || v < 15) {
        out.data[o + 3] = 0;
        continue;
      }

      const [r, g, b] = rampColour(v);
      out.data[o] = r;
      out.data[o + 1] = g;
      out.data[o + 2] = b;
      const t = (Math.min(v, 95) - 15) / (95 - 15);
      out.data[o + 3] = Math.round((ALPHA_MIN + (ALPHA_MAX - ALPHA_MIN) * t) * 255);
    }
  }

  ctx.putImageData(out, 0, 0);
  return {
    url: canvas.toDataURL("image/png"),
    coordinates: [
      [west, north],
      [east, north],
      [east, south],
      [west, south],
    ],
  };
}
