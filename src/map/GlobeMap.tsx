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
 * ⚠️ SYMBOL LAYERS FOR ICONS, DOM MARKERS FOR TEXT, and the line between them is a
 * real constraint rather than a preference. MapLibre needs a `glyphs` endpoint only
 * when a symbol layer carries a `text-field`; the default for that endpoint is a
 * remote URL, which would quietly reintroduce the external dependency this file
 * exists to avoid. An ICON-ONLY symbol layer needs no glyphs at all, so icons go
 * through symbol layers (see ./icons.ts) and get rotation, overlap control and
 * correct occlusion behind the globe. Anything with text stays a DOM marker.
 *
 * ⚠️ This paragraph previously read "NO SYMBOL LAYERS, DELIBERATELY". That was true
 * when the only symbol layer anyone wanted had labels on it, and it stayed in the
 * file for one commit after icons.ts made it false.
 */
import { useEffect, useRef } from "react";
// MapLibre v6 removed the default export; everything is a named import now.
import { config as maplibreConfig, Map as MapLibreMap, type GeoJSONSource } from "maplibre-gl";
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

import { useStore } from "../store";
import { viewportBbox } from "./bounds";
import { buildIcons, iconImageExpression, ICON_PIXEL_RATIO } from "./icons";

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
  degraded: "#ffd166",
  silent: "#8a4a4a",
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
  const ready = useRef(false);

  const projection = useStore((s) => s.projection);
  const setBbox = useStore((s) => s.setBbox);

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
          // Declared here with empty data rather than added later, so it can sit
          // between the ocean and the land in the layer order. Ice is on the sea and
          // under the coastline, and getting that order wrong makes the archipelago
          // vanish under a blue wash every March.
          ice: { type: "geojson", data: emptyFC() },
        },
        layers: [
          { id: "ocean", type: "background", paint: { "background-color": COLOR.ocean } },
          {
            // 🔑 COLOUR IS CONCENTRATION, AND IT CLAIMS NOTHING ELSE.
            //
            // This layer used to colour by "can a heavy vehicle cross this", derived from
            // a modelled thickness. That model is gone and so is the claim: what is drawn
            // now is the fraction of sea surface the satellite measured as ice-covered,
            // and nothing is inferred from it.
            //
            // ⚠️ Dense ice is brighter, which is the intuitive direction, but the ramp
            // starts at 15% because that is the standard threshold for "ice edge" in every
            // published extent figure. Below it, cells are dropped rather than drawn faint,
            // so the edge on screen is the same edge NSIDC counts.
            id: "ice",
            type: "fill",
            source: "ice",
            paint: {
              "fill-antialias": false,
              "fill-color": [
                "case",
                // The pole hole is UNMEASURED, not ice-free, and gets its own colour so it
                // never reads as either ice or open water.
                ["<", ["get", "concentration"], 0], COLOR.icePoleHole,
                [
                  "interpolate", ["linear"], ["get", "concentration"],
                  15, COLOR.iceThin,
                  60, COLOR.iceMid,
                  95, COLOR.iceDense,
                ],
              ],
              "fill-opacity": [
                "case",
                ["<", ["get", "concentration"], 0], 0.18,
                ["interpolate", ["linear"], ["get", "concentration"], 15, 0.12, 95, 0.34],
              ],
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
      m.addSource("asset-lines", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "asset-lines",
        type: "line",
        source: "asset-lines",
        paint: {
          "line-color": [
            "match",
            ["get", "kind"],
            "patrol", COLOR.patrol,
            "vessel", ["case", ["get", "dark"], COLOR.vesselDark, COLOR.vesselAis],
            COLOR.node,
          ],
          "line-width": ["case", ["get", "dark"], 1.6, 1.0],
          // A track held only by a sensor is drawn dashed, because it is inferred
          // rather than reported. The distinction is the whole point of the contact.
          "line-dasharray": ["case", ["get", "dark"], ["literal", [2, 2]], ["literal", [1, 0]]],
          "line-opacity": 0.55,
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
      m.addSource("asset-points", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "asset-status-ring",
        type: "circle",
        source: "asset-points",
        // Nominal assets get no ring at all. Drawing one in the background colour
        // would still cost a halo around every icon on the map and make a busy
        // display busier for no information.
        filter: ["!=", ["get", "status"], "nominal"],
        paint: {
          "circle-radius": 13,
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": [
            "match", ["get", "status"],
            "silent", COLOR.silent,
            "degraded", COLOR.degraded,
            "warning", COLOR.vesselDark,
            COLOR.degraded,
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
        // A silent asset fades rather than vanishing: last known position is still
        // information, and losing it off the screen is how an operator forgets a
        // node exists.
        "icon-opacity": ["match", ["get", "status"], "silent", 0.45, 1],
      };
      const MOVING_KINDS = ["vessel", "uas", "patrol"];

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

      // Icons must exist before the layer that names them is added, or MapLibre logs
      // "image not found" once per feature per frame and draws nothing.
      //
      // ⚠️ `ready` is set only after this resolves. The subscription below bails while
      // it is false, so the first store update after the icons land is what paints.
      buildIcons()
        .then((icons) => {
          for (const [id, bitmap] of icons) {
            if (!m.hasImage(id)) m.addImage(id, bitmap, { pixelRatio: ICON_PIXEL_RATIO });
          }
          ready.current = true;
          setBbox(viewportBbox(m));
          // Nudge the store so the layers paint immediately rather than at the next
          // clock tick, which would leave the map blank for up to a frame interval.
          useStore.setState((s) => ({ ...s }));
        })
        .catch((err) => {
          // A failed icon build must not leave a blank map with no explanation. The
          // rest of the display (land, graticule, links) is still useful.
          console.error("icon build failed", err);
          useStore.getState().setError(`icons failed to build: ${String(err)}`);
          ready.current = true;
          setBbox(viewportBbox(m));
        });
    });

    // The bbox is the command layer's answer to "the current zoom window", so it
    // is recomputed whenever the camera settles rather than on every frame.
    m.on("moveend", () => setBbox(viewportBbox(m)));

    return () => {
      m.remove();
      map.current = null;
      ready.current = false;
    };
  }, [setBbox]);

  // ---- projection toggle ---------------------------------------------------
  useEffect(() => {
    const m = map.current;
    if (!m || !ready.current) return;
    m.setProjection({ type: projection === "globe" ? "globe" : "mercator" });
  }, [projection]);

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

  // ---- redraw the live layers whenever the clock moves ---------------------
  useEffect(() => {
    const unsub = useStore.subscribe((s) => {
      const m = map.current;
      if (!m || !ready.current) return;

      // Assets. Converted from domain objects to GeoJSON here and nowhere else,
      // which is what keeps the store free of map-library shapes.
      const dark = (a: (typeof s.assets)[number]) => a.aisReporting === false;
      setData(
        m,
        "asset-points",
        s.assets
          .filter((a) => a.lat !== null && a.lon !== null)
          .map((a) =>
            point([a.lon as number, a.lat as number], {
              id: a.id,
              name: a.name,
              kind: a.kind,
              status: a.status,
              dark: dark(a),
              heading: headingOf(a),
            }),
          ),
      );
      setData(
        m,
        "asset-lines",
        s.assets
          .filter((a) => a.geometry !== null)
          .flatMap((a) =>
            splitAtAntimeridian(a.geometry!.coordinates).map((f) => ({
              ...f,
              properties: { id: a.id, kind: a.kind, dark: dark(a) },
            })),
          ),
      );

      // Sea ice for the selected date. Replaced wholesale rather than diffed: it is one
      // GeoJSON blob from the server and the map's own tiler handles the rest.
      if (s.ice) {
        const src = m.getSource("ice") as GeoJSONSource | undefined;
        src?.setData(s.ice.grid as never);
      }

      // Mesh links. Endpoints are looked up from the asset list rather than sent as
      // coordinates, so a link can never draw to a stale position: the graph names ids,
      // and ids resolve against whatever the store currently holds.
      if (s.mesh) {
        const at = new globalThis.Map(
          s.assets.filter((a) => a.lat !== null).map((a) => [a.id, a] as const),
        );
        setData(
          m,
          "mesh-links",
          s.mesh.links.flatMap((l) => {
            const a = at.get(l.a);
            const b = at.get(l.b);
            if (!a || !b) return [];
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
      }

    });
    return unsub;
  }, []);

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
