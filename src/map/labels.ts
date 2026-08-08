/**
 * Asset name labels, rasterised to images.
 *
 * 🔴 THIS EXISTS TO PUT TEXT ON THE MAP WITHOUT A GLYPH ENDPOINT, and that constraint is
 * the whole design. MapLibre needs `glyphs` only when a symbol layer carries a
 * `text-field`; the usual value for it is a remote URL, which would reintroduce exactly
 * the runtime network dependency the renderer is built to avoid. Setting `glyphs: ""` to
 * mean "there is no glyph source" does not work either: it fails URL-template validation
 * and aborts the entire style load, leaving a blank canvas and two console errors.
 *
 * The way out is that an ICON-ONLY symbol layer needs no glyphs at all. So each name is
 * drawn once into a canvas and registered as an image, and the label layer is an icon
 * layer whose `icon-image` is chosen per feature. The text is real text drawn by the
 * browser's own font stack; it simply arrives as pixels rather than as glyph ranges.
 *
 * 🥇 WHAT THIS BUYS, AND IT IS THE REASON IT BEAT THE ALTERNATIVE. Labels drawn as DOM
 * markers would need their own collision handling, their own occlusion test for the far
 * side of the globe, and their own per-frame position update. As a symbol layer, MapLibre
 * does all three: `icon-allow-overlap: false` gives real collision detection with a
 * declared priority order, and a symbol behind the globe is hidden by the same code that
 * hides the icons.
 *
 * ⚠️ THE COST, STATED RATHER THAN DISCOVERED: a rasterised label does not reflow, so it
 * is drawn at a fixed size and registered at pixel ratio 2. It stays crisp to roughly 2x
 * device scaling and softens beyond that. Names here are short and upper-case, so this is
 * a fair trade; a map that needed arbitrary user text at arbitrary zoom would want the
 * glyph pipeline instead.
 */

/** Authored height of one label image, in CSS pixels before supersampling. */
const HEIGHT = 16;
/** Rasterised at 2x and registered with pixelRatio 2, matching the icons. */
const SCALE = 2;
const FONT_PX = 10.5;
/** Breathing room around the text, so the plate does not crop the glyphs. */
const PAD_X = 4;

const FONT = `600 ${FONT_PX * SCALE}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;

/** Text colour by asset state. Mirrors the icon palette rather than inventing a second one. */
const TEXT = {
  normal: "#c9d6e2",
  /** The one red on the display: a contact held without an AIS broadcast. */
  dark: "#ff5c5c",
  /** Faded to match a silent asset's icon opacity, so the pair reads as one thing. */
  silent: "#7f8c99",
};

/**
 * A dark plate behind the text.
 *
 * Without it a label over the bright ice layer or a coastline is unreadable, and the
 * obvious fix, a text halo, is not available: a halo is a `text-*` property and there is
 * no text field here. A plate is also honest about the space the label occupies, which is
 * what MapLibre's collision boxes are measured against.
 */
const PLATE = "rgba(5, 10, 16, 0.72)";

export const LABEL_PIXEL_RATIO = SCALE;

export interface LabelStyle {
  dark: boolean;
  silent: boolean;
}

/**
 * The image id for one asset's label.
 *
 * 🔑 THE STATE IS PART OF THE ID, and that is what keeps a rasterised label honest. The
 * colour is baked into the pixels, so a contact that stops broadcasting would otherwise
 * keep a label in the old colour until the page reloaded. Encoding the state in the id
 * means the change of state names a different image, and the effect that draws the assets
 * registers whichever one it needs. At most four variants per asset, and only the ones
 * actually reached are ever built.
 */
export function labelImageId(assetId: string, style: LabelStyle): string {
  return `label:${assetId}:${style.dark ? "d" : "_"}${style.silent ? "s" : "_"}`;
}

/**
 * Which label image a feature should draw, as a MapLibre expression.
 *
 * 🔒 Kept here beside `labelImageId` deliberately: the two have to produce byte-identical
 * strings, and a mismatch shows up as MapLibre logging "image not found" once per feature
 * per frame. Two definitions in two files is how that drifts.
 */
export function labelImageExpression(): unknown[] {
  return [
    "concat",
    "label:",
    ["get", "id"],
    ":",
    ["case", ["==", ["get", "dark"], true], "d", "_"],
    // 🔴 KEYED ON `ring`, NOT ON `status`, AND THAT IS NOT INTERCHANGEABLE. It read
    // `status === "silent"` while the id builder had moved on to asking whether the ring
    // was `unreachable`. The two then produced different strings, the layer asked for an
    // image nobody had registered, and EVERY LABEL ON THE MAP SILENTLY STOPPED DRAWING.
    // This is precisely the drift the comment above warns about; it happened anyway,
    // because the two live in one file but were changed for different reasons.
    ["case", ["==", ["get", "ring"], "unreachable"], "s", "_"],
  ];
}

/**
 * Draw one label to `ImageData`.
 *
 * Synchronous on purpose. The icons decode through an `<img>` and so have to be awaited,
 * which forced a whole ready-flag dance in the map component. Text needs no decode step,
 * so a label can be built inline in the effect that is about to draw it, and there is
 * never a frame where the layer names an image that does not exist yet. That matters:
 * MapLibre logs "image not found" once per feature per frame for a missing image, which
 * would turn one typo into thousands of console errors and fail the console-error test.
 */
export function labelImage(text: string, style: LabelStyle): ImageData {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2d context available to rasterise labels");

  // Measure first, then size the canvas to fit. Setting width/height RESETS the context,
  // including the font, so the font has to be set again afterwards. Losing that line
  // produces labels in the browser default font and is invisible until you look closely.
  ctx.font = FONT;
  const textWidth = ctx.measureText(text).width;
  canvas.width = Math.ceil(textWidth + PAD_X * 2 * SCALE);
  canvas.height = HEIGHT * SCALE;

  ctx.font = FONT;
  ctx.textBaseline = "middle";

  ctx.fillStyle = PLATE;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.fillStyle = style.dark ? TEXT.dark : style.silent ? TEXT.silent : TEXT.normal;
  ctx.fillText(text, PAD_X * SCALE, canvas.height / 2 + 0.5 * SCALE);

  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}
