"""GET /api/window - the propagated future, in one request.

WHY A WINDOW INSTEAD OF A LIVE FEED. The obvious design for a moving map is a
WebSocket pushing positions at 1 Hz. That is not available here: this runs on
serverless functions, which have no long-lived process and no persistent
connection. Rather than fake it with 1 Hz polling, the server hands the client a
slice of the future and the client plays it back on its own clock.

That turned out to be the better design regardless:

  * One request replaces 1,800 messages.
  * "Fast forward to the next satellite event" becomes free, because the client
    already holds the events. Under a live feed it would need a second query.
  * Pass boundaries stay EXACT. Satellite positions are sampled coarsely and
    interpolated for display, but link state is sent as INTERVALS with AOS and
    LOS bisected to under a second. If link state were sampled too, then
    fast-forwarding to a precise AOS would land on a sample that still read
    "down", and the link would snap up as much as a sample-step late, which is
    exactly the moment anyone watching is looking at.

So: positions are approximate between samples and honestly so. Events are exact.
The client never computes orbital geometry; it interpolates between given points
and compares a clock against given intervals, which is presentation, not physics.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from _lib import passes as passlib
from _lib import satellites as satlib
from _lib import seed

# Sampling step for satellite positions. A 780 km satellite moves ~70 km in 10 s,
# which sounds like a lot until you convert it: 0.63 degrees along track, with a
# chord-versus-arc error around 100 m. Invisible at any zoom this application
# offers, and it keeps the payload small.
SAMPLE_STEP_S = 10.0

# Clamped at the endpoint, not in the tool validator, because this is a raw GET
# that anything can call. A window is cheap but not free: cost is linear in
# minutes and in satellite count.
MAX_WINDOW_MINUTES = 180
DEFAULT_WINDOW_MINUTES = 30


def build_window(start: datetime, minutes: float) -> dict:
    end = start + timedelta(minutes=minutes)
    sats = seed.seed_satellites()
    sites = seed.seed_sites()

    # Positions: sampled.
    tracks = []
    for sat in sats:
        samples = []
        t = start
        while t <= end:
            sp = satlib.subpoint_at(sat, t)
            samples.append(
                [
                    round((t - start).total_seconds(), 1),
                    round(sp.lat_deg, 4),
                    round(sp.lon_deg, 4),
                    round(sp.alt_km, 1),
                ]
            )
            t += timedelta(seconds=SAMPLE_STEP_S)
        tracks.append({"satellite_id": sat.id, "name": sat.name, "samples": samples})

    # Link state: exact intervals, not samples. See the module docstring.
    links = []
    for site in sites:
        for sat in sats:
            for p in passlib.find_passes(
                sat, site.geodetic, site.id, start, end, mask_deg=seed.DEFAULT_MASK_DEG
            ):
                links.append(p.as_dict())

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sample_step_s": SAMPLE_STEP_S,
        "mask_deg": seed.DEFAULT_MASK_DEG,
        "sites": [
            {
                "id": s.id,
                "name": s.name,
                "lat": s.lat_deg,
                "lon": s.lon_deg,
                "alt_m": s.alt_m,
                "kind": s.kind,
            }
            for s in sites
        ],
        "tracks": tracks,
        "passes": links,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - name fixed by the runtime
        query = parse_qs(urlparse(self.path).query)

        raw_from = (query.get("from") or [None])[0]
        try:
            start = (
                datetime.fromisoformat(raw_from.replace("Z", "+00:00"))
                if raw_from
                else datetime.now(timezone.utc)
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            return self._send(400, {"error": "from must be an ISO 8601 instant"})

        try:
            minutes = float((query.get("minutes") or [DEFAULT_WINDOW_MINUTES])[0])
        except ValueError:
            return self._send(400, {"error": "minutes must be a number"})
        minutes = max(1.0, min(MAX_WINDOW_MINUTES, minutes))

        try:
            payload = build_window(start, minutes)
        except satlib.PropagationError as exc:
            return self._send(500, {"error": str(exc)})

        return self._send(200, payload)

    def _send(self, status: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep function logs clean
        pass
