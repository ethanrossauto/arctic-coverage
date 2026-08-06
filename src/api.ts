/**
 * The API client. One function per endpoint, and the wire format is translated
 * into the app's own types here rather than anywhere else.
 *
 * Why translate at the boundary: the server sends compact arrays
 * (`[t, lat, lon, alt]`) to keep the payload small, and the rest of the frontend
 * should never see that. If the wire format changes for performance reasons, this
 * file changes and nothing else does.
 */
import type { PassInterval, Sample, Track } from "./playback";

export interface Site {
  id: string;
  name: string;
  lat: number;
  lon: number;
  altM: number;
  kind: string;
}

export interface Window {
  start: number; // ms since epoch
  end: number;
  sampleStepS: number;
  maskDeg: number;
  sites: Site[];
  tracks: Track[];
  passes: PassInterval[];
}

interface WireWindow {
  start: string;
  end: string;
  sample_step_s: number;
  mask_deg: number;
  sites: { id: string; name: string; lat: number; lon: number; alt_m: number; kind: string }[];
  tracks: { satellite_id: string; name: string; samples: [number, number, number, number][] }[];
  passes: {
    satellite_id: string;
    site_id: string;
    aos: string;
    los: string;
    tca: string;
    max_elevation_deg: number;
    duration_s: number;
  }[];
}

export async function fetchWindow(from: Date, minutes: number): Promise<Window> {
  const params = new URLSearchParams({
    from: from.toISOString(),
    minutes: String(minutes),
  });
  const res = await fetch(`/api/window?${params}`);
  if (!res.ok) {
    throw new Error(`window request failed: ${res.status} ${await res.text()}`);
  }
  const w = (await res.json()) as WireWindow;

  return {
    start: Date.parse(w.start),
    end: Date.parse(w.end),
    sampleStepS: w.sample_step_s,
    maskDeg: w.mask_deg,
    sites: w.sites.map((s) => ({
      id: s.id,
      name: s.name,
      lat: s.lat,
      lon: s.lon,
      altM: s.alt_m,
      kind: s.kind,
    })),
    tracks: w.tracks.map((t) => ({
      satelliteId: t.satellite_id,
      name: t.name,
      samples: t.samples.map(
        ([time, lat, lon, altKm]): Sample => ({ t: time, lat, lon, altKm }),
      ),
    })),
    passes: w.passes.map(
      (p): PassInterval => ({
        satelliteId: p.satellite_id,
        siteId: p.site_id,
        aos: Date.parse(p.aos),
        los: Date.parse(p.los),
        tca: Date.parse(p.tca),
        maxElevationDeg: p.max_elevation_deg,
        durationS: p.duration_s,
      }),
    ),
  };
}
