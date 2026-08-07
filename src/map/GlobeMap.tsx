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
 *   3. It reads as an operations display rather than as a web map with satellites
 *      drawn on top, which is what it is.
 *
 * ⚠️ MAPLIBRE 6.1+ IS REQUIRED, NOT PREFERRED. Every v5 release rendered GeoJSON
 * layers twice near the antimeridian when zoomed out or looking at a pole
 * (maplibre-gl-js#6248). A zoomed-out polar view of local GeoJSON is this app's
 * opening shot, so on v5 the bug would be the first thing anyone saw.
 *
 * ⚠️ NO SYMBOL LAYERS, DELIBERATELY. MapLibre's `symbol` type needs a glyph
 * endpoint, and the default is a remote URL, which would quietly reintroduce the
 * external dependency this file exists to avoid. Labels are DOM markers instead.
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

import { linksAt, useStore } from "../store";
import { positionAt } from "../playback";
import { viewportBbox } from "./bounds";

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
  site: "#7fe3c0",
  siteDown: "#4a6b7c",
  satellite: "#ffd166",
  link: "#7fe3c0",
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
        // Omitting the key is how you actually have no glyph source, and it is
        // safe here because nothing uses a symbol layer.
        sources: {
          land: { type: "geojson", data: "/data/land.json" },
          graticule: { type: "geojson", data: "/data/graticule.json" },
        },
        layers: [
          { id: "ocean", type: "background", paint: { "background-color": COLOR.ocean } },
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
      for (const [id, spec] of [
        ["links", { type: "line", paint: { "line-color": COLOR.link, "line-width": 1.4, "line-opacity": 0.85 } }],
        ["ground-tracks", { type: "line", paint: { "line-color": COLOR.satellite, "line-width": 1, "line-opacity": 0.35, "line-dasharray": [2, 2] } }],
      ] as const) {
        m.addSource(id, { type: "geojson", data: emptyFC() });
        m.addLayer({ id, source: id, ...(spec as object) } as never);
      }
      for (const [id, color, radius] of [
        ["sites", COLOR.site, 5],
        ["satellites", COLOR.satellite, 4],
      ] as const) {
        m.addSource(id, { type: "geojson", data: emptyFC() });
        m.addLayer({
          id,
          type: "circle",
          source: id,
          paint: {
            "circle-radius": radius,
            "circle-color": ["case", ["get", "linked"], color, COLOR.siteDown],
            "circle-stroke-width": 1,
            "circle-stroke-color": "#0b1219",
          },
        });
      }
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

      m.addSource("asset-points", { type: "geojson", data: emptyFC() });
      m.addLayer({
        id: "asset-points",
        type: "circle",
        source: "asset-points",
        paint: {
          "circle-radius": [
            "match", ["get", "kind"],
            "patrol", 5.5, "uas", 5, "vessel", 4.5, "hydrophone", 4, "radar", 3, 3.5,
          ],
          "circle-color": [
            "match", ["get", "kind"],
            "node", COLOR.node,
            "patrol", COLOR.patrol,
            "uas", COLOR.uas,
            "hydrophone", COLOR.hydrophone,
            "radar", COLOR.radar,
            "vessel", ["case", ["get", "dark"], COLOR.vesselDark, COLOR.vesselAis],
            COLOR.node,
          ],
          // Status overrides kind colour, because "this one is in trouble" must beat
          // "this one is a node" at a glance.
          "circle-stroke-color": [
            "match", ["get", "status"],
            "silent", COLOR.silent,
            "degraded", COLOR.degraded,
            "warning", COLOR.vesselDark,
            "#0b1219",
          ],
          "circle-stroke-width": ["match", ["get", "status"], "nominal", 1, 2.5],
          "circle-opacity": ["match", ["get", "status"], "silent", 0.35, 1],
        },
      });

      ready.current = true;
      setBbox(viewportBbox(m));
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

  // ---- redraw the live layers whenever the clock moves ---------------------
  useEffect(() => {
    const unsub = useStore.subscribe((s) => {
      const m = map.current;
      if (!m || !ready.current || !s.window) return;

      const elapsed = (s.simClock - s.window.start) / 1000;
      const active = linksAt(s.window, s.simClock);
      const linkedSites = new Set(active.map((p) => p.siteId));

      const satPositions = new globalThis.Map<string, { lat: number; lon: number }>();
      const satFeatures = [];
      for (const track of s.window.tracks) {
        const pos = positionAt(track, elapsed);
        if (!pos) continue;
        satPositions.set(track.satelliteId, pos);
        satFeatures.push(
          point([pos.lon, pos.lat], {
            id: track.satelliteId,
            name: track.name,
            linked: active.some((p) => p.satelliteId === track.satelliteId),
          }),
        );
      }
      setData(m, "satellites", satFeatures);

      setData(
        m,
        "sites",
        s.window.sites.map((site) =>
          point([site.lon, site.lat], {
            id: site.id,
            name: site.name,
            linked: linkedSites.has(site.id),
          }),
        ),
      );

      // A link is drawn only while it is up, and "up" comes from exact interval
      // boundaries rather than from sampled elevation, so it appears on the frame
      // the pass actually begins.
      setData(
        m,
        "links",
        active.flatMap((p) => {
          const site = s.window!.sites.find((x) => x.id === p.siteId);
          const sat = satPositions.get(p.satelliteId);
          if (!site || !sat) return [];
          return [
            line(
              [
                [site.lon, site.lat],
                [sat.lon, sat.lat],
              ],
              { siteId: p.siteId, satelliteId: p.satelliteId },
            ),
          ];
        }),
      );

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

      // Ground tracks: the whole window's path, so the shape of the orbit reads
      // even when nothing is currently linked.
      setData(
        m,
        "ground-tracks",
        s.window.tracks.flatMap((t) => splitAtAntimeridian(t.samples.map((x) => [x.lon, x.lat]))),
      );
    });
    return unsub;
  }, []);

  return <div ref={container} className="map" />;
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
