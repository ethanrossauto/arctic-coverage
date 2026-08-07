/**
 * Hand-drawn tactical icons, one per asset kind.
 *
 * WHY THESE ARE SVG STRINGS IN A .ts FILE rather than files in public/. They have to
 * become `ImageBitmap`s and be handed to `map.addImage()` before any symbol layer
 * referencing them is added, so they are needed synchronously at style-load time.
 * Keeping them inline removes eight fetches from the critical path and keeps the
 * no-external-fetch property that the rest of the renderer is built around.
 *
 * 🔑 WHY A SYMBOL LAYER IS USABLE HERE AT ALL. MapLibre needs a `glyphs` endpoint
 * only when a symbol layer has a `text-field`. An ICON-ONLY symbol layer needs no
 * glyphs, so this buys `icon-rotate` (heading on vessels and drones),
 * `icon-allow-overlap`, and correct occlusion behind the globe, none of which DOM
 * markers give, without reintroducing a remote dependency. Text labels are still DOM
 * markers, because those genuinely do need fonts.
 *
 * ⚠️ ONE IMAGE PER KIND, NOT ONE PER KIND-AND-STATE. The kind colour is baked into
 * the SVG; status is carried by a circle layer drawn UNDERNEATH and by `icon-opacity`
 * above. Baking status in would mean 8 kinds x 4 states = 32 images that all have to
 * agree with each other, and the first time one drifted nobody would notice.
 *
 * ⚠️ NOT SDF ICONS. `addImage(..., {sdf: true})` would allow `icon-color` and so one
 * image per kind tinted at runtime, but MapLibre reads the alpha channel as a signed
 * distance field, and a plain rasterised alpha mask is not one. Edges go soft and
 * uneven at anything but the authored size. Full-colour images cost nothing here.
 *
 * SHAPE CARRIES KIND, COLOUR CARRIES STATE. That split is deliberate: it is what lets
 * the display stay readable for someone who cannot rely on colour, and it is why the
 * silhouettes below are distinguishable in outline alone.
 *
 * A NOTE ON THE SYMBOLOGY. These borrow the grammar of standard military symbology
 * without claiming to implement it: friendly things get rounded frames, the contact
 * that is not broadcasting gets a distinctly different silhouette. Enough that the
 * conventions read as familiar, not so much that it claims to be MIL-STD-2525 and
 * then gets the details wrong.
 */

/** Authored size. Rasterised at 2x and registered with pixelRatio 2, so it stays sharp. */
const BOX = 32;
const SCALE = 2;

/**
 * Kind colours. Deliberately the same values the circle layers already used, so that
 * replacing dots with icons is not also a palette change: one variable at a time.
 */
export const KIND_COLOR: Record<string, string> = {
  node: "#7fe3c0",
  patrol: "#9be15d",
  uas: "#5ec8f2",
  launch_site: "#ffd166",
  hydrophone: "#6c8cff",
  radar: "#5a6b7a",
  vessel: "#d8dee9",
  // A contact holding no AIS broadcast is the only red on the display. It gets its
  // own image rather than a tint, because it also gets its own silhouette.
  vessel_dark: "#ff5c5c",
  marker: "#c9d4e0",
};

/** Dark backing so an icon punches out over land as well as over water. */
const BACKING = "#0b1219";

/**
 * Each entry returns the INNER markup of a 32x32 SVG. Stroke width is authored for
 * this box; do not scale these paths by hand, change BOX and let the rasteriser do it.
 */
const SHAPES: Record<string, (c: string) => string> = {
  /** Mesh sensor node: a guyed mast with two transmission arcs. Static, so no rotation. */
  node: (c) => `
    <path d="M16 25 V11" stroke="${c}" stroke-width="2.2" stroke-linecap="round"/>
    <path d="M11 27 L16 21 L21 27 Z" fill="${c}"/>
    <path d="M10.5 12.5 A 8 8 0 0 1 21.5 12.5" fill="none" stroke="${c}" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M7 9 A 13 13 0 0 1 25 9" fill="none" stroke="${c}" stroke-width="1.5" stroke-linecap="round" opacity="0.75"/>
    <circle cx="16" cy="10" r="2" fill="${c}"/>`,

  /**
   * Ranger patrol: the friendly-frame rectangle with an infantry cross. The one icon
   * here that is close to real symbology, because a ground unit is the case the
   * convention is most recognisable for.
   */
  patrol: (c) => `
    <rect x="5.5" y="10" width="21" height="12" rx="2.5"
          fill="${BACKING}" stroke="${c}" stroke-width="2.2"/>
    <path d="M6.5 11 L25.5 21 M6.5 21 L25.5 11" stroke="${c}" stroke-width="1.6"/>`,

  /** UAS: swept planform, nose UP so `icon-rotate` maps straight onto heading. */
  uas: (c) => `
    <path d="M16 3.5 L19 13 L28.5 18.5 L28.5 21.5 L18.5 18.8 L17.6 24.5
             L20.5 27.5 L16 26.4 L11.5 27.5 L14.4 24.5 L13.5 18.8
             L3.5 21.5 L3.5 18.5 L13 13 Z"
          fill="${c}" stroke="${BACKING}" stroke-width="0.8" stroke-linejoin="round"/>`,

  /** Forward launch site: a pad, and something leaving it. */
  launch_site: (c) => `
    <path d="M5.5 27 L26.5 27 L23 20.5 L9 20.5 Z"
          fill="${BACKING}" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>
    <path d="M10.5 17.5 L16 8 L21.5 17.5" fill="none" stroke="${c}"
          stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M13 22.5 L19 22.5" stroke="${c}" stroke-width="1.6" stroke-linecap="round" opacity="0.8"/>`,

  /**
   * Hydrophone: the ice line is the whole point of the icon. Everything below it is
   * under the ice, which is the one thing about this asset a reader has to grasp.
   */
  hydrophone: (c) => `
    <path d="M3.5 10.5 H28.5" stroke="${c}" stroke-width="2.2" stroke-linecap="round"/>
    <path d="M8 8.6 L10 10.4 M15 8.6 L17 10.4 M22 8.6 L24 10.4"
          stroke="${c}" stroke-width="1.2" stroke-linecap="round" opacity="0.7"/>
    <path d="M16 10.5 V17.5" stroke="${c}" stroke-width="1.8"/>
    <circle cx="16" cy="20.5" r="3.2" fill="${c}"/>
    <path d="M10.5 25.5 A 7 7 0 0 0 21.5 25.5" fill="none" stroke="${c}"
          stroke-width="1.5" stroke-linecap="round" opacity="0.8"/>`,

  /** Vessel holding an AIS broadcast: plan-view hull, bow UP for `icon-rotate`. */
  vessel: (c) => `
    <path d="M16 4 L21 13.5 V25 H11 V13.5 Z"
          fill="${BACKING}" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>
    <path d="M11.5 17.5 H20.5" stroke="${c}" stroke-width="1.4" opacity="0.85"/>`,

  /**
   * Vessel with NO AIS. Same hull so it still reads as a ship, broken outline because
   * the track is inferred rather than reported, and a query mark where the identity
   * would be. It is the only icon on the display allowed to be red.
   */
  vessel_dark: (c) => `
    <path d="M16 4 L21 13.5 V25 H11 V13.5 Z"
          fill="${BACKING}" stroke="${c}" stroke-width="2"
          stroke-linejoin="round" stroke-dasharray="3.5 2.2"/>
    <path d="M14 15.5 a2.2 2.2 0 1 1 2.4 3 v1.4" fill="none" stroke="${c}"
          stroke-width="1.7" stroke-linecap="round"/>
    <circle cx="16.4" cy="22.4" r="1.05" fill="${c}"/>`,

  /**
   * Early-warning radar. Desaturated by its palette entry, because this is existing
   * infrastructure the deployable layer works alongside rather than owns, and it must
   * read as background.
   */
  radar: (c) => `
    <path d="M11.5 27 L16 15 L20.5 27" fill="none" stroke="${c}"
          stroke-width="1.8" stroke-linecap="round"/>
    <path d="M13.5 22 H18.5" stroke="${c}" stroke-width="1.3"/>
    <path d="M8.5 13.5 A 8.5 8.5 0 0 1 23.5 13.5" fill="none" stroke="${c}"
          stroke-width="2.2" stroke-linecap="round"/>
    <path d="M16 15 V9" stroke="${c}" stroke-width="1.5"/>
    <path d="M24.5 8.5 A 12 12 0 0 0 20 5" fill="none" stroke="${c}"
          stroke-width="1.3" stroke-linecap="round" opacity="0.65"/>`,

  /** Operator-placed marker. The plainest thing on the map, on purpose. */
  marker: (c) => `
    <circle cx="16" cy="16" r="5.5" fill="none" stroke="${c}" stroke-width="1.8"/>
    <path d="M16 4.5 V10 M16 22 V27.5 M4.5 16 H10 M22 16 H27.5"
          stroke="${c}" stroke-width="1.8" stroke-linecap="round"/>
    <circle cx="16" cy="16" r="1.4" fill="${c}"/>`,
};

/** Every image id this module registers. Exported so the map can assert they all arrived. */
export const ICON_IDS = Object.keys(SHAPES);

function svg(kind: string): string {
  const color = KIND_COLOR[kind] ?? "#ffffff";
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${BOX}" height="${BOX}" viewBox="0 0 ${BOX} ${BOX}">${SHAPES[kind](color)}</svg>`;
}

/**
 * Rasterise one icon to `ImageData`.
 *
 * 🔴 CHROMIUM CANNOT DECODE AN SVG WITH `createImageBitmap`, AND THAT IS THE OBVIOUS
 * ROUTE. Handing it a `Blob` of `image/svg+xml` rejects with
 * "InvalidStateError: The source image could not be decoded". Firefox implements it,
 * Chromium has never shipped it. The failure is easy to misread, because the map keeps
 * working and only the symbol layer is empty, so the first suspect is the layer
 * expression rather than the image that was never built. Verified here in headless
 * Chromium before this comment was written, not assumed.
 *
 * So: decode through an `<img>`, which HAS supported SVG forever, then draw to a
 * canvas at the supersampled size and read the pixels back.
 *
 * ⚠️ A DATA URL, NOT AN OBJECT URL. Both decode, but an object URL has to be revoked
 * and a data URL cannot leak. `encodeURIComponent` rather than `btoa`, because `btoa`
 * throws on any character outside Latin-1 and the day someone puts a degree sign in a
 * path is not the day to discover that.
 *
 * ⚠️ AN EXPLICIT width AND height ON THE SVG IS LOAD-BEARING. With only a viewBox, an
 * `<img>` has no intrinsic size, and `drawImage` with explicit destination dimensions
 * behaves inconsistently across engines. `svg()` above always sets both.
 *
 * ⚠️ `getImageData` THROWS ON A TAINTED CANVAS, which is the desired behaviour: an SVG
 * that reached out to an external resource would trip it, and this project's whole
 * renderer is built on fetching nothing at runtime. A silent tint would be worse than
 * a thrown error.
 */
async function raster(kind: string): Promise<ImageData> {
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg(kind))}`;
  const img = new Image(BOX, BOX);

  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error(`icon "${kind}" failed to decode as an image`));
    img.src = url;
  });

  const size = BOX * SCALE;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;

  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2d context available to rasterise icons");

  // Drawn at the supersampled size so the vector is rasterised there, rather than
  // rasterised small and scaled up, which is the difference between a crisp icon and
  // a soft one on a high-DPI screen.
  ctx.drawImage(img, 0, 0, size, size);
  return ctx.getImageData(0, 0, size, size);
}

/**
 * Build every icon. Call once, await it, and only then add the symbol layers.
 *
 * 🔒 FAILS LOUDLY, AND CHECKS THAT SOMETHING WAS ACTUALLY DRAWN. A fully transparent
 * result means the SVG parsed but produced nothing, which renders exactly like a data
 * problem and sends you debugging the wrong half of the app. Cheaper to catch here,
 * where the message can name the icon.
 */
export async function buildIcons(): Promise<Map<string, ImageData>> {
  const out = new Map<string, ImageData>();
  await Promise.all(
    ICON_IDS.map(async (kind) => {
      const data = await raster(kind);
      if (data.width === 0 || data.height === 0) {
        throw new Error(`icon "${kind}" rasterised to ${data.width}x${data.height}`);
      }
      if (!hasOpaquePixels(data)) {
        throw new Error(`icon "${kind}" rasterised fully transparent: check its SVG paths`);
      }
      out.set(kind, data);
    }),
  );
  return out;
}

/** Any pixel at all above a low alpha threshold. Sampled, because this runs on startup. */
function hasOpaquePixels(data: ImageData): boolean {
  for (let i = 3; i < data.data.length; i += 4 * 8) {
    if (data.data[i] > 8) return true;
  }
  return false;
}

export const ICON_PIXEL_RATIO = SCALE;

/**
 * Which image a feature should draw.
 *
 * Kept here beside the shapes rather than inline in the layer definition, so that
 * adding a kind means editing one file. Returned as a MapLibre `match` expression.
 */
export function iconImageExpression(): unknown[] {
  return [
    "case",
    // A vessel's identity, not its kind, decides its silhouette.
    ["==", ["get", "dark"], true], "vessel_dark",
    ["match", ["get", "kind"], ...ICON_IDS.filter((k) => k !== "vessel_dark").flatMap((k) => [k, k]), "marker"],
  ];
}
