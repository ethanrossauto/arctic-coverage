/**
 * The renderer. MapLibre GL JS in globe projection, drawing a tactical display.
 *
 * NO BASEMAP TILES, ANYWHERE. Land, the graticule and the Arctic Circle are local
 * GeoJSON files served from this app. Three reasons, in order of how much they
 * matter:
 *
 *   1. Nothing external can fail during a demo. There is no tile provider to rate
 *      limit us, no key to expire, no third party to be down at the wrong moment.
 *   2. Over the Canadian Arctic a street-level basemap shows almost nothing. What
 *      carries meaning here is the coastline, the grid, and where the assets are.
 *   3. It reads as an operations display rather than as a web map with markers
 *      drawn on top, which is what it is.
 *
 * ⚠️ MAPLIBRE 6.1+ IS REQUIRED, NOT PREFERRED. Every v5 release rendered GeoJSON
 * layers twice near the antimeridian when zoomed out or looking at a pole
 * (maplibre-gl-js#6248). A zoomed-out polar view of local GeoJSON is this app's
 * opening shot, so on v5 the bug would be the first thing anyone saw.
 *
 * ⚠️ SYMBOL LAYERS THROUGHOUT, AND NO `text-field` ANYWHERE. MapLibre needs a `glyphs`
 * endpoint only when a symbol layer carries a `text-field`; the default for that
 * endpoint is a remote URL, which would quietly reintroduce the external dependency
 * this file exists to avoid. An ICON-ONLY symbol layer needs no glyphs at all, so
 * everything drawn at a point goes through one, and gets rotation, overlap control and
 * correct occlusion behind the globe for free.
 *
 * ⚠️ THAT INCLUDES THE NAME LABELS, which are text drawn to a canvas and registered as
 * images. See ./labels.ts for why that beat the two obvious alternatives. This paragraph
 * used to end "Anything with text stays a DOM marker", which was the right call while
 * the only text on the map was hypothetical and the wrong one once it had to collide
 * with 67 other labels.
 *
 * ⚠️ It previously read "NO SYMBOL LAYERS, DELIBERATELY". That was true when the only
 * symbol layer anyone wanted had labels on it, and it stayed in the file for one commit
 * after icons.ts made it false.
 */
import { useEffect, useRef, useState } from "react";
// MapLibre v6 removed the default export; everything is a named import now.
import {
  config as maplibreConfig,
  Map as MapLibreMap,
  type GeoJSONSource,
  type ImageSource,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// 🔴 MapLibre parses ALL geometry in a web worker, and it must be told where that
// worker lives. Get this wrong and the map renders NOTHING while the rest of the
// page works perfectly and no exception is thrown, which is how a blank globe
// reached the live site once already.
//
// v6 ships the worker as TWO files: maplibre-gl-worker.mjs imports
// maplibre-gl-shared.mjs by a relative path at runtime. Neither is reachable via
// the package's main entry, so a bundler that inlines the entry emits neither.
// scripts/copy-maplibre-worker.mjs copies both into public/maplibre/ side by side,
// so the worker's own import resolves. Read that file for the two approaches that
// failed first.
//
// The path is absolute and identical in dev and production, deliberately: the
// earlier fixes behaved differently in the two, and that difference is what let a
// broken build ship.
maplibreConfig.WORKER_URL = "/maplibre/maplibre-gl-worker.mjs";

import {
  isGateway,
  isUnreachable,
  KIND_LABEL,
  ringState,
  unknownState,
  weakAssetIds,
} from "../assets";
import { runCommand } from "../commandRunner";
import { useStore } from "../store";
import { viewportBbox } from "./bounds";
import { buildIcons, iconImageExpression, ICON_PIXEL_RATIO } from "./icons";
import { buildIceTexture } from "./iceTexture";
import { labelImage, labelImageExpression, labelImageId, LABEL_PIXEL_RATIO } from "./labels";

const COLOR = {
  ocean: "#050a10",
  // Asset palette. Own assets sit in the green/blue family; CONTACTS are
  // deliberately outside it, and a contact that is not broadcasting is the only
  // thing on the display allowed to be red. Colour carries meaning here rather
  // than decoration, which is why it is one table read top to bottom.
  node: "#7fe3c0",
  patrol: "#9be15d",
  uas: "#5ec8f2",
  hydrophone: "#6c8cff",
  // Existing infrastructure, not owned kit: deliberately desaturated so it reads as
  // background against the deployable layer rather than competing with it.
  radar: "#5a6b7a",
  vesselAis: "#d8dee9",
  vesselDark: "#ff5c5c",
  // Queried position history.
  //
  // 🔑 A HUE NOTHING ELSE ON THIS DISPLAY USES, and that is the whole selection criterion.
  // Amber was the first choice and was wrong: it is already the degraded status ring and
  // the launch-site icon, so a trail would have shared a colour with two things that mean
  // a condition. Every other colour here encodes what an asset IS or how it is DOING. A
  // trail encodes neither; it is the answer to a question somebody just typed, which is a
  // different category and earns its own hue. It also has to stay legible over dark ocean,
  // dark land and bright ice, which rules out anything pale.
  track: "#c792ea",
  // A sensor hold: which sensor can see which contact.
  //
  // 🔑 DESATURATED ON PURPOSE, and it is the same argument the radar colour makes. This
  // line is evidence about a contact, not a condition of one, so it must not compete with
  // the red that means "not broadcasting" nor with the green family that means our kit.
  // Dotted carries the meaning; the colour only has to stay legible over ocean and ice.
  detection: "#8fa6bd",
  // ---- the three condition rings, and nothing else uses these -------------
  /** Maintenance: the kit needs attention and we can still hear it. */
  maintenance: "#ffd166",
  /** Weak: still connected, but its best link has almost no margin left. */
  weak: "#ff9f43",
  /** Unreachable: no live path to a gateway, so the icon greys out with the ring. */
  unreachable: "#6b7683",
  /** What the operator clicked. */
  selected: "#7fe3c0",
  /** What the last command's answer points at. Warmer than selection so the two read apart. */
  highlight: "#ffd166",
  degraded: "#ffd166",
  land: "#111b26",
  coast: "#2b4258",
  graticule: "#16242f",
  arcticCircle: "#2d4a52",
  // Ice. Brightest where a heavy vehicle can cross, because that is the only one of the
  // three that changes what an operator can do.
  iceThin: "#4a6070",
  iceMid: "#7ba8bf",
  iceDense: "#cfe8f5",
  // The satellite cannot see the pole. Its own colour, because rendering an unmeasured
  // area as either ice or ocean would be inventing a measurement.
  icePoleHole: "#3a3f52",
};

/** Where the camera starts: straight down at the pole, the whole Arctic in frame. */
const INITIAL_VIEW = { center: [-95, 74] as [number, number], zoom: 2.1 };

export function GlobeMap() {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  // State rather than a ref, deliberately: the layer effects below have to RUN when the
  // icons finish building, and a ref changing wakes nothing up. That is what the old
  // `useStore.setState(s => ({...s}))` nudge was standing in for.
  const [ready, setReady] = useState(false);

  const projection = useStore((s) => s.projection);
  const setBbox = useStore((s) => s.setBbox);

  // 🔑 ONE SELECTOR PER LAYER. Each of these drives its own effect below, so a change
  // to one never re-uploads the others. See the note above the effects.
  // ⚠️ THE DRAWN POSITIONS, NOT THE REPORTED ONES. `displayAssets` is the same set carried
  // forward between five second fixes so movement is continuous rather than a jump per
  // poll; see `useDeadReckoning`. Everything that COUNTS or ANSWERS still reads `assets`,
  // because an estimate must never become a number anybody quotes.
  const assets = useStore((s) => s.displayAssets);
  const mesh = useStore((s) => s.mesh);
  const placing = useStore((s) => s.placing);
  const ice = useStore((s) => s.ice);
  const showIce = useStore((s) => s.showIce);
  const hideUndetected = useStore((s) => s.hideUndetected);
  const hiddenKinds = useStore((s) => s.hiddenKinds);
  const track = useStore((s) => s.track);

  // ---- create the map once -------------------------------------------------
  useEffect(() => {
    if (!container.current || map.current) return;

    const m = new MapLibreMap({
      container: container.current,
      style: {
        version: 8,
        // ⚠️ NO `glyphs` KEY AT ALL, and this is the second attempt at it. Setting
        // `glyphs: ""` to signal "there is no glyph source" fails: MapLibre
        // validates the value as a URL template, rejects it for missing the
        // {fontstack} and {range} tokens, and ABORTS THE WHOLE STYLE LOAD. The
        // result is a blank canvas with two console errors and no other symptom.
        // Omitting the key is how you actually have no glyph source, and it is safe
        // because the symbol layers below are icon-only: `glyphs` is required for
        // `text-field`, and nothing here has one.
        sources: {
          land: { type: "geojson", data: "/data/land.json" },
          graticule: { type: "geojson", data: "/data/graticule.json" },
          // 🔴 AN IMAGE SOURCE, NOT GEOJSON, and that is what stopped it looking pixelated.
          // A fill layer draws one polygon per cell, and polygons have hard edges however
          // small you make them, so the grid is always visible. A raster layer hands the
          // grid to the GPU as a texture and `raster-resampling: linear` interpolates
          // between cells for free.
          //
          // Declared here rather than added later so it can sit between the ocean and the
          // land in the layer order: ice is on the sea and under the coastline, and getting
          // that order wrong makes the archipelago vanish under a blue wash every March.
          //
          // ⚠️ A 1x1 TRANSPARENT PIXEL UNTIL REAL DATA ARRIVES. An image source must have a
          // valid url when it is added, and the coordinates are patched in with the first
          // real frame, once the grid header says where the grid actually is.
          ice: {
            type: "image",
            url:
              "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" +
              "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
            coordinates: [
              [-180, 89],
              [180, 89],
              [180, 55],
              [-180, 55],
            ],
          },
        },
        layers: [
          { id: "ocean", type: "background", paint: { "background-color": COLOR.ocean } },
          {
            // 🔑 COLOUR IS CONCENTRATION, AND IT CLAIMS NOTHING ELSE. What is drawn is the
            // fraction of sea surface the satellite measured as ice-covered on this date.
            // Nothing is inferred from it and no thickness is implied.
            //
            // The colour ramp lives in buildIceTexture() rather than here, because MapLibre
            // 6.2 has no `raster-color`: the paint properties are brightness, contrast,
            // hue-rotate, saturation, opacity, resampling and fade. Read out of the shipped
            // bundle rather than assumed, after a plan that depended on it.
            id: "ice",
            type: "raster",
            source: "ice",
            paint: {
              // 🥇 THE ONE LINE THAT FIXED THE PIXELATION. `nearest` is what a fill layer
              // effectively gives you; `linear` is the GPU interpolating between measured
              // cells, which is a visual treatment of a real measurement rather than
              // invented data.
              "raster-resampling": "linear",
              // Per-pixel alpha is baked into the texture from concentration, so the layer
              // itself is fully opaque and the ramp decides what shows through.
              "raster-opacity": 1,
              // No cross-fade. Changing month is a jump between two measurements, and
              // dissolving between them would put a blend of two dates on screen.
              "raster-fade-duration": 0,
            },
          },
          {
            id: "land",
            type: "fill",
            source: "land",
            paint: { "fill-color": COLOR.land, "fill-outline-color": COLOR.coast },
          },
          {
            id: "graticule",
            type: "line",
            source: "graticule",
            filter: ["!=", ["get", "kind"], "arctic_circle"],
            paint: { "line-color": COLOR.graticule, "line-width": 0.5 },
          },
          {
            id: "arctic-circle",
            type: "line",
            source: "graticule",
            filter: ["==", ["get", "kind"], "arctic_circle"],
            paint: {
              "line-color": COLOR.arcticCircle,
              "line-width": 1.2,
              "line-dasharray": [3, 3],
            },
          },
        ],
      },
      center: INITIAL_VIEW.center,
      zoom: INITIAL_VIEW.zoom,
      attributionControl: false,
    });

    map.current = m;

    m.on("style.load", () => {
      // ⚠️ setProjection only takes effect after the style has loaded. Called
      // earlier it is silently ignored and you get a flat map with no error.
      m.setProjection({ type: "globe" });

      // Live layers. Added here rather than in the style so their data can be
      // replaced every frame from the store.
      // ---- asset layers -------------------------------------------------
      // TWO layers for five kinds, not five layers, driven by data expressions.
      // A layer per kind would mean five sources to keep in step and five style
      // blocks that drift apart; the kind is data, so it belongs in the paint
      // expression rather than in the layer list.
      // ---- queried position history ---------------------------------------
      //
      // 🔒 THE ONLY LINE-PER-ASSET ON THIS MAP, AND IT IS DRAWN ONLY WHEN ASKED FOR.
      //
      // This replaced a layer that drew each asset's seeded route permanently. Eleven of
      // the sixty-eight carried one, so the display opened with lines trailing behind
      // vessels and patrols that nobody had asked about. Two things were wrong with that.
      // It is scenery, competing for attention with the two questions the console exists
      // to answer. And it meant the answer to "where has this been" would be drawn in the
      // same visual language as something that was already on screen, so the one command
      // that produces a line would have produced no visible change.
      //
      // Amber because it has to survive being drawn over dark ocean, dark land and bright
      // ice, and because it is the palette's attention colour: a trail here is the answer
      // to a question somebody just asked, which is exactly what should be pulling the
      // eye. Heavier and far more opaque than a mesh link, which is the other thin line
      // on this map, so the two never read as the same kind of thing.
      m.addSource("history-track", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "history-track",
        type: "line",
        source: "history-track",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": COLOR.track,
          "line-width": ["interpolate", ["linear"], ["zoom"], 2, 2.0, 6, 3.0],
          // Fully opaque, unlike every other line here. A mesh link is ambient and sits
          // back at 0.6; this is the answer to a question somebody just asked and should
          // sit in front of everything. It also means the colour on screen IS the colour
          // in this file, with no blend against whatever it happens to cross, which is
          // what makes it assertable.
          "line-opacity": 1,
        },
      });

      // Where the series STARTS. A bare line says where something went but not which end
      // it came from, and "oldest first" is a fact about the array that the picture
      // otherwise throws away.
      m.addSource("history-origin", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "history-origin",
        type: "circle",
        source: "history-origin",
        paint: {
          "circle-radius": 3.5,
          "circle-color": COLOR.track,
          "circle-stroke-color": COLOR.ocean,
          "circle-stroke-width": 1,
        },
      });

      // ---- the mesh link graph --------------------------------------------
      //
      // Drawn UNDER the assets so icons stay readable where a cluster is dense.
      //
      // 🔑 LINE COLOUR ENCODES MARGIN, NOT EXISTENCE. Every link here is up; what
      // differs is how close it is to not being up. A link with 0.6 km of headroom and
      // one with 20 km look identical on any display that only draws connectivity, and
      // they are completely different operationally: the first is what "about to be cut
      // off" actually looks like. Amber is under 3 km of margin.
      // 🔑 SENSOR HOLDS, DRAWN DOTTED SO THEY CANNOT BE READ AS RADIO LINKS. A solid line
      // means "these two can talk to each other"; a dotted one means "this sensor can see
      // that contact". They are different claims about different graphs, and drawing them
      // in one style would invite an operator to read a detection as connectivity.
      //
      // Added BELOW the mesh links deliberately: where both run between the same patch of
      // map, the network the operator owns is the one on top.
      m.addSource("detections", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "detections",
        type: "line",
        source: "detections",
        paint: {
          "line-color": COLOR.detection,
          "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.6, 6, 1.2],
          "line-opacity": 0.55,
          "line-dasharray": [2, 2],
        },
      });

      m.addSource("mesh-links", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "mesh-links",
        type: "line",
        source: "mesh-links",
        paint: {
          "line-color": [
            "interpolate", ["linear"], ["get", "margin"],
            0, COLOR.degraded,
            3, COLOR.degraded,
            8, COLOR.node,
          ],
          "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.7, 6, 1.6],
          "line-opacity": 0.6,
        },
      });

      // ---- assets: a status ring UNDER a hand-drawn icon ------------------
      //
      // Two layers over one source, and the split is what keeps the icon set small.
      // SHAPE carries kind (32 hand-drawn silhouettes would otherwise be needed for
      // kind x status); COLOUR carries state, and it is drawn underneath so it reads
      // as a condition applied TO the asset rather than as part of what the asset is.
      //
      // It also degrades honestly for anyone who cannot rely on colour: every kind is
      // still distinguishable in outline with the ring removed entirely.
      // `promoteId` lifts the asset id onto the FEATURE id, which is what makes
      // `setFeatureState` usable. Selection is then a per-feature flag the GPU reads,
      // rather than a property baked into the data, so clicking an asset does not
      // re-upload all 68 features to the worker.
      m.addSource("asset-points", { type: "geojson", data: emptyFC(), promoteId: "id" });

      // ---- selection ------------------------------------------------------
      // Under everything, so it reads as a spotlight the asset is standing in rather
      // than as another ring competing with the condition ring above it.
      m.addLayer({
        id: "asset-selected",
        type: "circle",
        source: "asset-points",
        paint: {
          "circle-radius": 20,
          "circle-color": COLOR.selected,
          "circle-opacity": [
            "case", ["boolean", ["feature-state", "selected"], false], 0.22, 0,
          ],
          "circle-stroke-color": COLOR.selected,
          "circle-stroke-width": 1,
          "circle-stroke-opacity": [
            "case", ["boolean", ["feature-state", "selected"], false], 0.9, 0,
          ],
        },
      });

      // ---- what a command's answer points at --------------------------------
      //
      // 🔑 SEPARATE FROM SELECTION, because they answer different questions. Selection is
      // "the one thing I clicked"; highlight is "every asset in the answer you just got".
      // A query can highlight nine assets while one of them is selected, and collapsing
      // the two would make "show me the drones" look like it clicked five things at once.
      //
      // Drawn as a wide soft halo rather than a ring, so it reads as illumination falling
      // on the answer rather than as another condition badge competing with the rings.
      m.addLayer({
        id: "asset-highlight",
        type: "circle",
        source: "asset-points",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 11, 6, 17],
          "circle-color": COLOR.highlight,
          "circle-opacity": [
            "case", ["boolean", ["feature-state", "highlighted"], false], 0.3, 0,
          ],
          "circle-blur": 0.55,
        },
      });

      // ---- contacts the network cannot confirm --------------------------------
      //
      // 🔑 REUSES THE MEANINGS THIS DISPLAY ALREADY HAS rather than inventing two colours.
      // `detected_not_reported` is a LINK fault, something is watching and cannot deliver,
      // which is exactly what the weak-link orange already says. `untracked` is a thing we
      // cannot hear about at all, which is what the unreachable grey already says. A
      // dashed ring, because dashed already means inferred rather than reported.
      m.addLayer({
        id: "asset-unknown",
        type: "circle",
        source: "asset-points",
        filter: ["!=", ["get", "unknown"], "none"],
        paint: {
          "circle-radius": 15,
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": [
            "match", ["get", "unknown"],
            "detected_not_reported", COLOR.weak,
            COLOR.unreachable,
          ],
          "circle-stroke-width": 1.4,
          "circle-stroke-opacity": 0.85,
        },
      });

      // ---- the condition ring ---------------------------------------------
      //
      // 🔑 THREE RINGS, THREE QUESTIONS, AND THE COLOURS ARE NOT DECORATION.
      //
      //   yellow  maintenance   the kit needs attention, and we can still hear it
      //   orange  weak          still connected, but its best link is nearly gone
      //   grey    unreachable   we are not hearing from it, so everything else is stale
      //
      // Nominal assets get no ring at all. Drawing one in the background colour would
      // still cost a halo around every icon and make a busy display busier for nothing.
      //
      // ⚠️ RED IS NOT IN THIS LIST, DELIBERATELY. The non-broadcasting contact is the only
      // red on the display, and that is what makes it findable in one glance across a
      // globe covered in dots. Weak takes orange instead: distinct from the yellow beside
      // it, and it leaves the one red meaning one thing.
      m.addLayer({
        id: "asset-status-ring",
        type: "circle",
        source: "asset-points",
        filter: ["!=", ["get", "ring"], "none"],
        paint: {
          "circle-radius": 13,
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": [
            "match", ["get", "ring"],
            "maintenance", COLOR.maintenance,
            "weak", COLOR.weak,
            "unreachable", COLOR.unreachable,
            COLOR.maintenance,
          ],
          "circle-stroke-width": 2,
          "circle-stroke-opacity": 0.9,
        },
      });

      // 🔴 TWO ICON LAYERS, AND THE SPLIT IS FORCED BY THE PROJECTION.
      //
      // `icon-rotation-alignment` is a LAYOUT property, so it cannot be data-driven:
      // one layer means one answer for every asset. And the two answers are genuinely
      // different here.
      //
      // A vessel's bow has to point where it is going, so it must rotate WITH the map
      // ("map"). But map-aligned icons also inherit the local direction of north, and
      // on a pole-centred globe north points at the centre of the screen from every
      // direction at once. So a mast, a radar tower and a hydrophone all splay
      // outward like spokes, which is correct behaviour and looks like a bug.
      //
      // Static kinds therefore align to the VIEWPORT and stay upright; moving kinds
      // align to the map and carry a heading. Caught from a screenshot, not from the
      // docs: on a mercator map both look identical and this never surfaces.
      // ⚠️ `["zoom"]` MAY ONLY BE THE INPUT TO A TOP-LEVEL `interpolate` or `step`.
      // Multiplying a per-kind factor by a zoom ramp, which is the obvious way to
      // write this, is rejected by the style validator at layer-add time and the
      // layer never appears. So the ramp is on the outside and the per-kind factor is
      // folded into each stop instead.
      const iconSize = (kindFactor: unknown): unknown[] => [
        "interpolate", ["linear"], ["zoom"],
        1.5, ["*", 0.5, kindFactor],   // whole Arctic in frame: a cluster reads as texture
        3.5, ["*", 0.75, kindFactor],
        6, ["*", 1.0, kindFactor],     // station zoom: full size, legible
      ];
      const iconPaint = {
        // 🔑 AN UNREACHABLE ASSET GREYS OUT RATHER THAN VANISHING. Its last known
        // position is still the best information anyone has, and losing it off the
        // screen is how an operator forgets a node exists. Fading says "this is where
        // it was when we last heard it", which is exactly true: the server stops
        // advancing a position it cannot hear, so a faded icon is also a stationary one.
        "icon-opacity": [
          "case",
          // Faded harder than an unreachable asset: this one may not be there at all.
          ["==", ["get", "unknown"], "untracked"], 0.3,
          ["==", ["get", "unknown"], "detected_not_reported"], 0.45,
          ["==", ["get", "ring"], "unreachable"], 0.45,
          1,
        ],
      };
      // Kinds that travel, and so align to the map and carry a heading. The two contact
      // kinds are here because an aircraft drawn without a heading points north while its
      // track runs east, which reads as a rendering fault rather than as missing data.
      const MOVING_KINDS = ["vessel", "uas", "patrol", "aircraft", "ground_party"];

      m.addLayer({
        id: "asset-icons-static",
        type: "symbol",
        source: "asset-points",
        filter: ["!", ["in", ["get", "kind"], ["literal", MOVING_KINDS]]],
        layout: {
          "icon-image": iconImageExpression() as never,
          "icon-size": iconSize(["match", ["get", "kind"], "radar", 0.72, "marker", 0.8, 0.9]) as never,
          // Everything is drawn. On a tactical display an asset silently suppressed by
          // collision detection is worse than two overlapping, because the operator
          // cannot tell "not there" from "not drawn".
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-rotation-alignment": "viewport",
        },
        paint: iconPaint as never,
      });

      m.addLayer({
        id: "asset-icons-moving",
        type: "symbol",
        source: "asset-points",
        filter: ["in", ["get", "kind"], ["literal", MOVING_KINDS]],
        layout: {
          "icon-image": iconImageExpression() as never,
          "icon-size": iconSize(0.9) as never,
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-rotation-alignment": "map",
          "icon-rotate": ["coalesce", ["get", "heading"], 0],
        },
        paint: iconPaint as never,
      });

      // ---- the backhaul badge ---------------------------------------------
      //
      // Its own layer rather than a variant silhouette, so a gateway still reads as
      // whatever kind it is and simply gains a mark. `icon-translate` is in screen pixels,
      // so the badge keeps the same offset from its icon at every zoom.
      m.addLayer({
        id: "asset-gateway",
        type: "symbol",
        source: "asset-points",
        filter: ["==", ["get", "gateway"], true],
        layout: {
          "icon-image": "gateway_badge",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 1.5, 0.6, 3.5, 0.8, 6, 1.0],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-rotation-alignment": "viewport",
        },
        paint: {
          "icon-translate": [17, -17],
          "icon-opacity": ["case", ["==", ["get", "ring"], "unreachable"], 0.45, 1],
        } as never,
      });

      // ---- the name beside every icon -------------------------------------
      //
      // 🔑 THE ONLY LAYER ON THIS MAP THAT COLLIDES, AND THE ONLY ONE THAT SHOULD.
      // The icon layers above set `icon-allow-overlap: true` because an asset silently
      // suppressed by collision detection is worse than two overlapping: the operator
      // cannot tell "not there" from "not drawn". A LABEL is the opposite case. It is a
      // convenience on top of a symbol that is already drawn, so dropping it costs
      // nothing but a name, and 68 overlapping names cost the whole display.
      //
      // Leaving overlap off is therefore what thins the labels by zoom, without a zoom
      // ramp anywhere: at the opening pole-centred view most of them collide and drop,
      // and they appear as you zoom into a cluster. MapLibre re-runs placement on every
      // camera change, so this is free.
      //
      // ⚠️ The icon layers do not participate. `icon-ignore-placement: true` up there
      // means an icon never blocks a label, so a label is only ever suppressed by
      // another label. Without that, dense clusters would lose their names to their own
      // icons.
      m.addLayer({
        id: "asset-labels",
        type: "symbol",
        source: "asset-points",
        layout: {
          "icon-image": labelImageExpression() as never,
          "icon-anchor": "left",
          "icon-allow-overlap": false,
          "icon-ignore-placement": false,
          "icon-padding": 3,
          // Which name survives a collision, lowest placed first. The two questions this
          // display exists to answer come first: what is not broadcasting, then what has
          // gone quiet. The unowned radar line yields to everything, because it is
          // background that happens to be numerous.
          "symbol-sort-key": [
            "case",
            ["==", ["get", "dark"], true], 0,
            ["!=", ["get", "status"], "nominal"], 1,
            ["==", ["get", "kind"], "radar"], 3,
            2,
          ],
        },
        paint: {
          // In pixels and independent of icon-size, which is what keeps the gap between
          // an icon and its name constant at every zoom. `icon-offset` would have been
          // multiplied by icon-size and drifted as the icons scale.
          "icon-translate": [13, 0],
          "icon-opacity": ["case", ["==", ["get", "ring"], "unreachable"], 0.72, 1],
        } as never,
      });

      // Icons must exist before the layer that names them is added, or MapLibre logs
      // "image not found" once per feature per frame and draws nothing.
      //
      // ⚠️ `ready` flips only after this resolves, and the layer effects below bail
      // while it is false. Flipping it is what paints the first frame.
      //
      // ⚠️ The `map.current !== m` guard matters in dev: StrictMode mounts twice, so a
      // torn-down map's icon build can still resolve and would otherwise mark the
      // REPLACEMENT map ready before its own icons exist.
      buildIcons()
        .then((icons) => {
          if (map.current !== m) return;
          for (const [id, bitmap] of icons) {
            if (!m.hasImage(id)) m.addImage(id, bitmap, { pixelRatio: ICON_PIXEL_RATIO });
          }
          setBbox(viewportBbox(m));
          setReady(true);
        })
        .catch((err) => {
          // A failed icon build must not leave a blank map with no explanation. The
          // rest of the display (land, graticule, links) is still useful.
          if (map.current !== m) return;
          console.error("icon build failed", err);
          useStore.getState().setError(`icons failed to build: ${String(err)}`);
          setBbox(viewportBbox(m));
          setReady(true);
        });
    });

    // The bbox is the command layer's answer to "the current zoom window", so it
    // is recomputed whenever the camera settles rather than on every frame.
    m.on("moveend", () => setBbox(viewportBbox(m)));

    // ---- the loading curtain is NOT lifted here ---------------------------
    //
    // 🔴 IT USED TO BE, ON `m.once("idle")`, AND THAT SIGNAL WAS WRONG IN A WAY THAT LOOKED
    // RIGHT. `idle` means MapLibre has drawn everything it currently can, and at this point
    // in the boot it currently can draw the basemap and nothing else: the assets have not
    // been fetched, so they are not a source yet and there is nothing outstanding for the
    // map to be busy with. Measured, that fires around 0.9 s while the icons do not paint
    // until about 4.3 s, so the curtain came up on an empty globe and the console looked
    // broken for three and a half seconds on every cold load.
    //
    // 🔑 IT IS LIFTED WHERE "loading" CLEARS, on the effect below that asks the map whether
    // the asset icons are literally on screen. That is the same question the curtain is
    // asking, so the two must not answer it separately: this file already carried two
    // different signals for one moment, and only one of them was correct.

    // ---- click to select, and ask when the click is ambiguous ------------
    //
    // 🔑 HIT-TESTED ON THE ICON LAYERS, NOT THE RING OR THE LABEL. The icon is the thing
    // that looks clickable, and it is the only one every asset has: a nominal asset wears
    // no ring, and its label may have been dropped by collision at the current zoom.
    // Hit-testing anything else would make some assets quietly unclickable.
    const ICON_LAYERS = ["asset-icons-static", "asset-icons-moving"];

    // 🔴 A BOX, NOT A POINT, AND THAT IS THE WHOLE FEATURE. Querying the single clicked
    // pixel answers "what is exactly under the cursor", which on a globe covered in
    // overlapping icons is a question about aim rather than about intent. An 8 pixel box
    // asks "what did they mean", finds every asset in the pile, and lets the operator say
    // which one. It also makes small icons at low zoom clickable at all.
    const SLOP = 8;

    m.on("click", (e) => {
      // 🔑 PLACING TAKES THE CLICK ENTIRELY, BEFORE ANY HIT TEST. With the PLACE control
      // armed, the operator has already said what this click is for, and running the
      // selection logic first would mean a click that happened to land on an existing icon
      // selected it instead of placing, which is the one thing they did not ask for.
      //
      // ⚠️ DISARMS ITSELF. Placing is a single act, not a mode you stay in: the alternative
      // is an operator who clicks twice and gets two nodes, and finds out from the map.
      const { placing, setPlacing } = useStore.getState();
      const { kind, unknown, backhaul } = placing;
      if (kind) {
        // Disarms the kind and keeps the flags, so placing three unknown contacts in a row
        // costs three clicks on the map and one trip back to the menu per kind change.
        setPlacing({ ...placing, kind: null });
        const lat = e.lngLat.lat;
        const lon = e.lngLat.lng;
        // ⚠️ THE SENTENCE IS FOR THE TRANSCRIPT AND IS NOT WHAT RUNS. The plan below is the
        // command; this is the line an operator reads back afterwards, so it has to say
        // what was actually done. `ui_button` is what tells the audit log a control did it
        // rather than someone speaking, which is a distinction worth keeping.
        const what = `${unknown ? "unknown " : ""}${KIND_LABEL[kind]}`;
        const article = /^[aeiou]/i.test(what) ? "an" : "a";
        const carrying = backhaul ? " with backhaul" : "";
        runCommand(
          `place ${article} ${what}${carrying} at ${lat.toFixed(3)}, ${lon.toFixed(3)}`,
          "ui_button",
          {
            // ⚠️ `params`, NOT `args`. The executor validates `step["params"]` against the
            // tool's declared parameters and treats a missing key as an empty dict, so the
            // wrong name does not error: the plan is accepted with no arguments, falls
            // through to the parser, and comes back asking for the kind and position that
            // were sitting right here. Silent, and it looked like a parser bug.
            plan: [
              {
                tool: "place_asset",
                params: {
                  kind,
                  lat,
                  lon,
                  // 🔑 BOTH FLAGS, BECAUSE THE CONTROL NOW SAYS HOSTILE. `unknown` makes it
                  // unidentified and silent; `hostile` is how it is regarded. A seeded
                  // UNKNOWN carries both, so sending only the first would place something
                  // the strip counts as unjudged under a control labelled hostile, which
                  // is the display asserting something it has not done.
                  ...(unknown ? { unknown: true, hostile: true } : {}),
                  ...(backhaul ? { backhaul: true } : {}),
                },
              },
            ],
          },
        );
        return;
      }

      const box: [[number, number], [number, number]] = [
        [e.point.x - SLOP, e.point.y - SLOP],
        [e.point.x + SLOP, e.point.y + SLOP],
      ];
      const hits = m.queryRenderedFeatures(box, { layers: ICON_LAYERS });

      // Deduplicated because one asset appears once per layer it matched, and the pile is
      // about distinct assets rather than about draw calls. Query order is render order,
      // topmost first, which is the order the operator's eye is already in.
      const ids: string[] = [];
      for (const f of hits) {
        const id = f.properties?.id;
        if (typeof id === "string" && !ids.includes(id)) ids.push(id);
      }

      const { select, setPicker } = useStore.getState();
      if (ids.length === 0) {
        // Clicking bare map clears both. Without this the banner is a thing you can open
        // and never close, which is worse than not having it.
        select(null);
        setPicker(null);
      } else if (ids.length === 1) {
        select(ids[0]);
        setPicker(null);
      } else {
        // ⚠️ The selection is NOT changed here. Opening the list while also selecting the
        // topmost would answer the question the list is asking.
        setPicker({ x: e.point.x, y: e.point.y, ids });
      }
    });

    // The list is anchored to a screen position, so it has to go the moment the world
    // underneath it moves. Panning with a pile still open would leave the names pointing
    // at whatever has since slid under them.
    m.on("movestart", () => useStore.getState().setPicker(null));

    // A pointer cursor is the only affordance saying these are interactive at all.
    for (const layer of ICON_LAYERS) {
      m.on("mouseenter", layer, () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", layer, () => (m.getCanvas().style.cursor = ""));
    }

    return () => {
      m.remove();
      map.current = null;
      setReady(false);
    };
  }, [setBbox]);

  // ---- projection toggle ---------------------------------------------------
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    m.setProjection({ type: projection === "globe" ? "globe" : "mercator" });
  }, [projection, ready]);

  // ---- a command asked the camera to move ----------------------------------
  /**
   * 🔑 The store carries a DOMAIN target (a latitude, a longitude, a zoom), never a
   * MapLibre camera object. That is the same rule the rest of the store follows, and it
   * is what lets the executor decide where to look without knowing what draws it.
   *
   * `flyTo` rather than `jumpTo` deliberately: when the camera moves in response to a
   * command, the movement is the feedback. A jump leaves the operator working out
   * whether the view changed or the data did.
   */
  const camera = useStore((s) => s.camera);
  useEffect(() => {
    const m = map.current;
    if (!m || !camera) return;
    m.flyTo({
      center: camera.center,
      zoom: camera.zoom ?? m.getZoom(),
      duration: 1200,
      essential: true,
    });
  }, [camera]);

  // ---- redraw each layer when ITS OWN data changes -------------------------
  /**
   * 🔴 ONE EFFECT PER SOURCE, AND THE SPLIT IS A PERFORMANCE FIX, NOT TIDINESS.
   *
   * This was a single `useStore.subscribe` with no selector, which fired on every write
   * to the store and called `setData` on all four sources each time. `setData` is not a
   * cheap assignment: MapLibre ships the GeoJSON to its worker, which re-parses and
   * re-tiles it. The ice layer alone is around 6,200 polygons on a dense March date.
   *
   * With a clock ticking from requestAnimationFrame, that ran at display refresh rate
   * for the whole session: a pinned worker thread, a `flyTo` that stuttered because the
   * worker was busy, and audible fan noise on a map of a world that was not moving. The
   * clock is gone now, but the subscription was the other half of it, and it would have
   * done the same thing to any future per-second write.
   *
   * Splitting it means selecting an asset redraws nothing, and appending a line to the
   * command transcript redraws nothing.
   */

  // Assets: the icons and the ground tracks. Converted from domain objects to GeoJSON
  // here and nowhere else, which is what keeps the store free of map-library shapes.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    // 🔴 WHAT EARNS RED IS BEING HELD AND UNIDENTIFIED, not being a vessel with its AIS
    // off. Those overlapped on the seeded world, so the rule read as "a dark vessel" for a
    // long time while an unidentified AIRCRAFT sat in the neutral grey every friendly asset
    // uses. `detectedUnknown` is computed on the server, where `tracked` and `ais_reporting`
    // are both in hand, precisely so the browser does not derive it a second way.
    //
    // ⚠️ DETECTED ONLY. An UNDETECTED unknown is deliberately untouched: the console cannot
    // legitimately claim to hold it, so it keeps the faded treatment that says exactly that.
    const dark = (a: (typeof assets)[number]) =>
      a.detectedUnknown === true || a.aisReporting === false;
    // Computed once per redraw rather than per asset: it walks the whole link list.
    const weak = weakAssetIds(mesh);
    const now = Date.now();

    // 🔴 REGISTER EVERY LABEL IMAGE BEFORE THE DATA THAT NAMES IT. A symbol layer whose
    // `icon-image` resolves to an id that was never added logs "image not found" once
    // per feature per frame, which is thousands of console errors and a failed
    // console-error test, and it draws nothing while looking like a data problem.
    //
    // Synchronous, so there is no window between the two. Labels rasterise through a
    // canvas with no decode step, unlike the icons, which have to go through an <img>.
    for (const a of assets) {
      const style = { dark: dark(a), silent: ringState(a, weak, now) === "unreachable" };
      const id = labelImageId(a.id, style);
      if (!m.hasImage(id)) {
        m.addImage(id, labelImage(a.name, style), { pixelRatio: LABEL_PIXEL_RATIO });
      }
    }

    setData(
      m,
      "asset-points",
      assets
        .filter((a) => a.lat !== null && a.lon !== null)
        // 🔒 THE DEFAULT PICTURE CLAIMS ONLY WHAT ARRIVED. An undetected unknown is not
        // drawn until the operator asks for it, because the line is whether the detection
        // reached this console: one of those buckets is held by nothing at all, and the
        // other is held by a sensor that cannot deliver, which leaves us in the same place.
        // Revealing them is a deliberate act, which is what the checkbox makes it.
        .filter((a) => !hideUndetected || unknownState(a) === null)
        // A display preference, applied last and kept apart from the honesty filter
        // above: that one decides what may be claimed, this one decides what one
        // operator wants to look at. Same effect on the map, different reasons, and
        // conflating them would make the VIEW menu look like it edits the truth.
        .filter((a) => !hiddenKinds.includes(a.kind))
        .map((a) =>
          point([a.lon as number, a.lat as number], {
            id: a.id,
            name: a.name,
            kind: a.kind,
            status: a.status,
            // "none" rather than null: MapLibre filters cannot compare against a missing
            // property, so the nominal case has to be a value like any other.
            ring: ringState(a, weak, now) ?? "none",
            gateway: isGateway(a),
            // "none" rather than null, because a MapLibre filter cannot compare against a
            // property that is absent.
            unknown: unknownState(a) ?? "none",
            dark: dark(a),
            heading: headingOf(a),
          }),
        ),
    );

  }, [assets, mesh, ready, hideUndetected, hiddenKinds]);

  // 🔴 "LOADING" USED TO CLEAR WHEN THE DATA ARRIVED, NOT WHEN IT WAS DRAWN. The store
  // cleared it inside `setAssets`, which fires the moment the fetch resolves, while MapLibre
  // still has to parse the features, rasterise the labels and paint a frame. Measured, the
  // canvas is up at about 0.9 s, so the notice vanished and left an empty globe looking
  // broken. `idle` is the map's own statement that it has nothing left to draw, which is the
  // only signal here that means what "loading" means to a person.
  //
  // ⚠️ ARMED ONCE, AND THE FIRST VERSION OF THIS WAS NOT. Sitting inside the data effect it
  // re-ran on every tick of the position interpolation, and each run cleared its own backstop
  // and re-armed it, so the timeout could never fire and the map was never idle either. The
  // notice then stayed up for ten seconds, which is worse than the bug it replaced. Keyed on
  // the COUNT rather than the array, and latched with a ref, so nothing after the first paint
  // can re-enter it.
  //
  // 🔒 THE BACKSTOP IS THE POINT. A notice that never clears is worse than one that clears
  // early, so a driver fault or a backgrounded tab gives up after a second and a half rather
  // than leaving the word on screen forever.
  const clearedLoading = useRef(false);
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || clearedLoading.current || !assets.length) return;
    clearedLoading.current = true;
    const done = () => {
      m.off("render", check);
      window.clearTimeout(backstop);
      useStore.getState().setLoading(false);
      // 🔑 THE CURTAIN COMES UP ON THE SAME SIGNAL, AND THAT IS THE WHOLE POINT OF PUTTING
      // IT HERE. "the operator can see the assets" is one moment, so it gets one answer.
      // Lifting the curtain on the map's first `idle` instead had it up at 0.9 s against
      // icons at 4.3 s, which is a blank globe with no explanation on screen.
      (window as { consoleReady?: () => void }).consoleReady?.();
    };
    // 🔑 ASK THE MAP WHETHER THE ICONS ARE ACTUALLY ON SCREEN. `idle` was the first attempt
    // and it is the wrong signal here: this map is a globe with a sea ice texture, so it
    // keeps rendering and does not go idle promptly, which turned the backstop into a fixed
    // delay and cleared the notice a second and a half LATE. Rendered features are the
    // literal question being asked, "can the operator see the assets yet".
    const check = () => {
      if (m.queryRenderedFeatures({ layers: ["asset-icons-static", "asset-icons-moving"] }).length) {
        done();
      }
    };
    m.on("render", check);
    // ⚠️ FAR BEYOND THE NORMAL PATH, DELIBERATELY. Measured on this machine the icons paint
    // about 1.9 s after the data lands, and an earlier 2.5 s here only beat that by half a
    // second: on a slower machine or a cold database the timer would win the race and clear
    // the notice early, which is the bug this effect exists to fix. A backstop that competes
    // with the thing it is backing up is not a backstop. Ten seconds catches a genuinely
    // stuck map and can never be the reason the word disappears on a working one.
    const backstop = window.setTimeout(done, 10_000);
    return () => {
      m.off("render", check);
      window.clearTimeout(backstop);
    };
  }, [assets.length, ready]);

  // 🔑 THE ARMED MAP HAS TO LOOK ARMED. A click behaves completely differently while PLACE
  // is armed, and a map that gives no sign of that turns the operator's next click into a
  // surprise. The crosshair is the conventional "you are about to put something here", and
  // it costs one line rather than an overlay.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    m.getCanvas().style.cursor = placing.kind ? "crosshair" : "";
  }, [placing, ready]);

  // The position history a command asked for, and nothing else. Null clears it, which is
  // what makes a trail belong to the question that produced it rather than accumulating.
  //
  // ⚠️ `a.geometry` is still read, by `headingOf` below, to point a vessel's bow the way
  // it is travelling. Not drawing the route does not mean throwing the route away.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;

    const coords = track?.coordinates ?? [];
    setData(m, "history-track", coords.length >= 2 ? splitAtAntimeridian(coords) : []);
    setData(
      m,
      "history-origin",
      coords.length >= 2 ? [point(coords[0], { id: track!.id })] : [],
    );
  }, [track, ready]);

  // Sea ice for the selected date, uploaded as one texture.
  //
  // 🥇 THIS USED TO BE TENS OF THOUSANDS OF POLYGONS and is now a single image. The layer
  // was a GeoJSON fill with one rectangle per cell, which is why it looked pixelated: a
  // polygon has a hard edge however small it is. Chasing that with more resolution took
  // the payload to 11.8 MB and made it worse, not better. A texture plus
  // `raster-resampling: linear` is the actual fix, and it is also far cheaper.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !ice) return;
    const src = m.getSource("ice") as ImageSource | undefined;
    if (!src) return;
    const { url, coordinates } = buildIceTexture(ice);
    // Coordinates travel with the image because the grid header decides where it belongs,
    // and the placeholder added at style time knows nothing about the real grid.
    src.updateImage({ url, coordinates });
  }, [ice, ready]);

  // Sensor holds: a dotted line from each sensor to the contact it is holding.
  //
  // 🔒 REPORTED ONLY. A sensor holding something whose route home is down is the one case
  // this display refuses to draw: the console cannot legitimately know that contact is
  // there, and a line to it would be the map asserting knowledge that never arrived. The
  // pair is shipped with the flag so the coverage view can still reveal it behind its own
  // control; the default picture claims only what reached us.
  //
  // ⚠️ THE SAME ENDPOINT RULES AS THE MESH LINKS, and for the same reasons: a hidden kind
  // drops its lines rather than running them to empty water, and an endpoint we cannot hear
  // from drops them rather than drawing to a position we are no longer sure of.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !mesh) return;
    const at = new globalThis.Map(
      assets
        .filter((a) => a.lat !== null && !hiddenKinds.includes(a.kind))
        .map((a) => [a.id, a] as const),
    );
    const now = Date.now();
    setData(
      m,
      "detections",
      (mesh.detections ?? []).flatMap((d) => {
        if (!d.reported) return [];
        const s = at.get(d.sensorId);
        const c = at.get(d.contactId);
        if (!s || !c) return [];
        if (isUnreachable(s, now)) return [];
        return [
          line(
            [
              [s.lon as number, s.lat as number],
              [c.lon as number, c.lat as number],
            ],
            { distance: d.distanceKm },
          ),
        ];
      }),
    );
  }, [assets, mesh, ready, hiddenKinds]);

  // Mesh links. Endpoints are looked up from the asset list rather than sent as
  // coordinates, so a link can never draw to a stale position: the graph names ids, and
  // ids resolve against whatever the store currently holds. That lookup is why this
  // depends on `assets` as well as on `mesh`.
  //
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !mesh) return;
    const at = new globalThis.Map(
      // 🔑 HIDDEN KINDS ARE ABSENT FROM THIS LOOKUP, WHICH IS WHAT DROPS THEIR LINKS.
      // A link whose endpoint is not drawn would otherwise run to empty water, which
      // reads as a fault in the mesh rather than as a filter the operator applied.
      assets
        .filter((a) => a.lat !== null && !hiddenKinds.includes(a.kind))
        .map((a) => [a.id, a] as const),
    );
    const now = Date.now();
    setData(
      m,
      "mesh-links",
      mesh.links.flatMap((l) => {
        const a = at.get(l.a);
        const b = at.get(l.b);
        if (!a || !b) return [];
        // 🔑 A LINK TO SOMETHING WE CANNOT HEAR IS NOT A LINK WE KNOW IS UP. The server
        // computes the graph from last known positions, so a link to a silent asset is a
        // statement about where it used to be. Drawing it claims live connectivity to
        // something that has stopped answering, which is the opposite of what the grey
        // treatment on that asset is saying two layers up.
        if (isUnreachable(a, now) || isUnreachable(b, now)) return [];
        return [
          line(
            [
              [a.lon as number, a.lat as number],
              [b.lon as number, b.lat as number],
            ],
            { margin: l.marginKm, distance: l.distanceKm },
          ),
        ];
      }),
    );
  }, [mesh, assets, ready, hiddenKinds]);

  // Selection, pushed to the GPU as feature state rather than into the data.
  //
  // 🔑 THIS IS WHY THE SOURCE HAS `promoteId`. Baking `selected` into the GeoJSON would
  // mean re-serialising all 68 features and re-parsing them in the worker on every click,
  // for a change that alters two circles. Feature state changes the paint result without
  // touching the data at all.
  //
  // ⚠️ The previous id is cleared explicitly. Feature state is not exclusive: setting a
  // new one does not unset the old, and the symptom is a display that accumulates
  // highlights until every asset is selected.
  const selectedId = useStore((s) => s.selectedId);
  const lastSelected = useRef<string | null>(null);
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    if (lastSelected.current && lastSelected.current !== selectedId) {
      m.setFeatureState({ source: "asset-points", id: lastSelected.current }, { selected: false });
    }
    if (selectedId) {
      m.setFeatureState({ source: "asset-points", id: selectedId }, { selected: true });
    }
    lastSelected.current = selectedId;
  }, [selectedId, ready, assets]);

  // Highlight, pushed as feature state for the same reason selection is: a query that
  // lights up nine assets must not re-serialise all 76 features to do it.
  //
  // ⚠️ The previous set is cleared explicitly. Feature state is not exclusive, so without
  // this a display accumulates highlights until every asset is lit and the effect stops
  // meaning anything.
  const highlightIds = useStore((s) => s.highlightIds);
  const lastHighlight = useRef<string[]>([]);
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    for (const id of lastHighlight.current) {
      if (!highlightIds.includes(id)) {
        m.setFeatureState({ source: "asset-points", id }, { highlighted: false });
      }
    }
    for (const id of highlightIds) {
      m.setFeatureState({ source: "asset-points", id }, { highlighted: true });
    }
    lastHighlight.current = highlightIds;
  }, [highlightIds, ready, assets]);

  // Mesh visibility. A `visibility` layout change rather than emptying the source: the
  // geometry stays parsed and uploaded, so turning it back on costs nothing and the data
  // effect above keeps its one job.
  //
  // ✅ Verified by diffing frames across an off/on cycle, cropped to the map area:
  // hiding changed 267 pixels and showing put every one of them back, exactly 0 different
  // from the original frame.
  //
  // ⚠️ MEASURE THE MAP, NOT THE SCREENSHOT. `.map` is `inset: 0`, so a screenshot of it
  // also contains the header, command bar, timebar and footer drawn on top. Counting
  // those made the checkbox's own pixels and an input focus ring read as map content,
  // and produced a confident, entirely wrong conclusion that this API was broken.
  // 🔑 THE MESH IS ALWAYS DRAWN AND HAS NO TOGGLE. It used to have a checkbox in the header,
  // on the reasoning that its lines are what you turn off to read a dense cluster. That was
  // true and it was still the wrong trade: the mesh is what makes this a network picture
  // rather than a map with icons on it, and a control that hides the subject earns its space
  // only if somebody actually wants it hidden. The layer is added with no `visibility`
  // property, so visible is its default and nothing has to assert it.

  // The ice layer, toggled the same way and for the same reason. This is what
  // "show me the weather overlays" reaches: a layer the client already draws.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    m.setLayoutProperty("ice", "visibility", showIce ? "visible" : "none");
  }, [showIce, ready]);

  return <div ref={container} className="map" />;
}

/**
 * Which way an icon should point, in degrees clockwise from north.
 *
 * Reported heading wins. Failing that it is derived from the last leg of the asset's
 * own track, which is what makes a vessel's bow agree with the line drawn behind it
 * rather than being a second, disagreeing claim about the same motion.
 *
 * ⚠️ THIS IS PRESENTATION MATHS, NOT DOMAIN GEOMETRY, and the distinction is the one
 * Rule 1 turns on. It decides how to draw a shape that is already positioned; it
 * decides nothing about where anything is. Same category as the slerp used to ease
 * between samples. Everything about position, range and connectivity stays server-side.
 */
function headingOf(a: { kind: string; props: Record<string, unknown>; geometry: { coordinates: [number, number][] } | null }): number {
  // 🔑 THE SERVER'S NUMBER, WHICH IS NOW THE COMPUTED ONE. `motion.heading_of` derives this
  // from the leg the asset is actually on and falls back to a reported heading only when
  // there is no route. Preferring the stored value here is what drew two ships more than 55
  // degrees off their own course: the number was seeded once and the route turned.
  const reported = a.props?.heading_deg;
  if (typeof reported === "number" && Number.isFinite(reported)) return reported;

  const coords = a.geometry?.coordinates;
  if (coords && coords.length >= 2) {
    const [lon1, lat1] = coords[coords.length - 2];
    const [lon2, lat2] = coords[coords.length - 1];
    return bearing(lat1, lon1, lat2, lon2);
  }
  return 0;
}

/** Initial great-circle bearing. Rhumb bearing would be wrong by degrees at 74N. */
function bearing(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = Math.PI / 180;
  const [p1, p2, dl] = [lat1 * r, lat2 * r, (lon2 - lon1) * r];
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) / r + 360) % 360;
}

// ---- small GeoJSON helpers, kept here so the store never holds map shapes ----

function emptyFC() {
  return { type: "FeatureCollection", features: [] } as never;
}

function point(coordinates: [number, number], properties: object) {
  return { type: "Feature", properties, geometry: { type: "Point", coordinates } };
}

function line(coordinates: [number, number][], properties: object) {
  return { type: "Feature", properties, geometry: { type: "LineString", coordinates } };
}

function setData(m: MapLibreMap, id: string, features: unknown[]) {
  const src = m.getSource(id) as GeoJSONSource | undefined;
  src?.setData({ type: "FeatureCollection", features } as never);
}

/**
 * Break a path into segments wherever it crosses the antimeridian.
 *
 * Without this a ground track drawn as one LineString sprints back across the
 * whole map at every wrap, which is the same failure the playback interpolation
 * avoids on the sphere. Here it has to be handled by splitting, because a
 * LineString's vertices are what they are.
 */
function splitAtAntimeridian(coords: [number, number][]) {
  const out: ReturnType<typeof line>[] = [];
  let run: [number, number][] = [];
  for (let i = 0; i < coords.length; i++) {
    if (i > 0 && Math.abs(coords[i][0] - coords[i - 1][0]) > 180) {
      if (run.length > 1) out.push(line(run, {}));
      run = [];
    }
    run.push(coords[i]);
  }
  if (run.length > 1) out.push(line(run, {}));
  return out;
}
