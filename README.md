# Arctic Coverage

A situational-awareness console for deployable Arctic sensor networks: where the assets are, which of
them have gone quiet, and which contacts are moving through the Northwest Passage without announcing
themselves.

**Live:** https://coverage.skryer.ca

The Canadian Arctic is watched today by a radar line built in the 1980s. A line is a tripwire. This
models the other approach: a mesh of cheap deployable sensors clustered at the places a transit cannot
avoid, plus the mobile assets and the contacts that make that picture mean something.

---

## What it does today

- **A 3D globe** centred on the Canadian Arctic, drawn as an operations display rather than a web map.
- **Seven kinds of asset**, 68 of them, each with genuinely different geometry, mobility and status.
- **Live satellite geometry**: three real satellites propagated from orbital elements, with exact
  acquisition and loss times against a 15 degree elevation mask.
- **A playback clock** you can pause and jump forward on, so events that are minutes apart can be
  watched in seconds.
- **An audit-log schema** that every state change is designed to write through.

**Not built yet:** the natural-language command layer. The schema, the viewport contract and the audit
log exist to serve it, and it is the next thing in.

---

## Quickstart

```bash
# back end
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn api.index:app --port 8000

# front end, in a second terminal
npm install && npm run dev          # http://127.0.0.1:5173
```

The database is Postgres. Point `DATABASE_URL` at one and seed it:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/seed_db.py     # idempotent; re-run any time
```

Tests:

```bash
npm test          # Playwright, node:test, pytest
```

---

## Architecture

```
React + MapLibre                         Python (ASGI, one FastAPI app)
  globe, panels, playback clock  --GET--> /api/window     propagation, exact pass intervals
  viewport bbox                  --GET--> /api/entities   the world
                                 --GET--> /api/events     the audit log
                                              |
                                          Postgres
```

Three rules hold the thing together, and each exists because breaking it caused a real problem.

### 1. The front end never computes domain geometry

Propagation, look angles, slant range, mask tests and pass finding all happen in Python. The browser
draws what it is handed.

The word *domain* is load-bearing. The front end unavoidably computes **camera** geometry, and it
interpolates between server-provided samples for animation. Neither is domain geometry. The line is
worth holding because it keeps one source of truth for anything a person might act on.

### 2. Buttons and language drive one registry through one executor

Every capability is a named, validated function. The UI calls it; the command layer will call the same
one. Nothing is implemented twice, and because the executor writes an audit row before any effect is
visible, the log is complete by construction rather than by discipline.

### 3. The client store holds domain objects, never map-library shapes

Entities with latitude and longitude, links as endpoint pairs, tracks as arrays of points. No GeoJSON
and no style config in application state. That is what makes the renderer replaceable: the globe is a
projection setting on the same map instance, and a mercator toggle is one line.

---

## Data model

Two tables. The shape is a position, not a default.

**One `entities` table for every kind, not a table per kind.** The kinds differ a lot, but the shared
core (id, kind, name, position, status) is what almost every query filters on and what the map draws.
A table per kind turns "what is near here" into a seven-way union, and that is most queries. Per-kind
detail lives in `props jsonb`.

**Two type-specific fields are promoted to real columns anyway.** `last_heard` and `ais_reporting`
drive the only two questions the system is really for: what has gone quiet, and what is not
broadcasting. Left in `jsonb` they would need a cast at every call site to be filterable. That is a
tradeoff of indexability against schema purity, and for exactly two fields, purity loses.

**The audit log has no foreign key to `entities`, deliberately.** Both obvious behaviours destroy its
purpose: `on delete cascade` erases the record along with the row, and `on delete set null` keeps the
record and drops the identity, leaving "something was deleted" with no way to say what. So
`events.entity_id` stays plain text after the row it names is gone.

> A referential constraint is the right default for data describing the present, and the wrong one for
> data describing the past. History does not get to change when the present does.

The cost is real: nothing at the database level stops `entity_id` naming something that never existed.
That is accepted, because every write goes through one function, and a typo there is a bug to fix
whereas a cascade is a design that silently discards evidence.

**The indexes do nothing at this row count, and the schema says so.** `EXPLAIN` returns a sequential
scan at this size and the planner is right. They are declared for the shape of the queries, not for
today's performance. Claiming a speedup that `EXPLAIN` contradicts would be worse than having no index.

---

## Reading the display

The console is meant to be read in one glance by someone who already knows the region, so the visual
grammar is deliberate and narrow.

- **Status beats kind in the paint order.** "This one is in trouble" has to win over "this one is a
  node", so status drives the outline and opacity while kind drives the fill.
- **One thing on screen is allowed to be red**: a contact that is not broadcasting. Everything else
  lives in a green-to-blue family, so the eye goes to the anomaly without being told to.
- **Existing radar sites are desaturated** and sit behind the deployable layer. They are infrastructure
  to work alongside, not owned assets, and they should read as background.
- **A track drawn dashed was inferred, not reported.** A contact held only by an acoustic sensor gets a
  dashed line, because the difference between "it told us where it was" and "we worked out where it
  was" is the entire point.
- **Two numbers sit in the status strip** rather than being folded into a total: how many assets are
  overdue, and how many contacts are not broadcasting. Those are what an operator actually watches.
- **No basemap tiles at all.** Land, the graticule and the Arctic Circle are local files. Over the
  Arctic a street-level basemap shows almost nothing, and nothing external can fail during a demo.

---

## Domain notes

**Why the assets sit where they sit.** The mesh clusters on the Northwest Passage chokepoints, because
a sensor is worth what passes it. The hydrophones are only in the narrows, because a sensor lowered
through the ice anywhere else is money spent watching open water. The drones are based at the real
northern airfields. Scattering them at random would produce the same screenshot and collapse under one
question from someone who knows the region.

**Why the satellites are polar, and why that is not cosmetic.** A ground track never reaches further
from the equator than its inclination, and at a 15 degree mask a satellite at 780 km is visible only
within about 1,737 km of a site. A 53-degree constellation never comes within 3,280 km of Alert, so it
would produce **zero** passes there. Not fewer: zero, forever, while every geometry test still passed,
because the physics would be correctly reporting no coverage. A separate test asserts that every
seeded site sees the constellation daily, since physics verification cannot catch a scenario error.

**Why satellites matter here at all.** Each mesh cluster reaches the outside world by satellite
backhaul. That is what lets clusters be locally isolated from one another and still be useful, and it
is why a coverage tool for a sensor network has an orbital half.

---

## Performance

- **One request replaces a stream.** Rather than pushing positions continuously, the server returns a
  window of the future in one response and the client plays it back on its own clock. A 30-minute
  window with three satellites and five sites is about 7 KB gzipped and 10 ms of compute.
- **Positions are sampled, events are exact.** Sample positions every 10 seconds and interpolate;
  send acquisition and loss times bisected to under a second. If link state were sampled too, jumping
  to a precise acquisition would land on a sample that still read "down" and the link would appear late
  by up to a sample step, which is exactly the moment anyone is watching.
- **Interpolation is spherical and linear.** A naive latitude/longitude interpolation sweeps a
  satellite the long way around the planet at every antimeridian crossing and makes it circle the pole
  rather than cross it. Every orbit here is near-polar, so that is the normal case.
- **Static assets come off a CDN, not through a function.** The basemap is the largest thing
  this app serves, and no request for it should wake a Python process.

---

## Testing

Three suites, all runnable without credentials.

- **Geometry, verified two ways.** The transform chain from orbital state to look angles is written by
  hand, so it is cross-checked against an independent implementation used as a test-only dependency:
  worst disagreement 37 m of ground position, 0.47 arcsec of elevation. Both sides are fed datetimes
  rather than a shared Julian date, so a broken conversion cannot cancel out of the comparison. The
  second layer is a pass time checked by hand against a public tracker, which catches errors both
  implementations could share.
- **Browser tests that answer one question**: does the page work. No golden images. A reference
  screenshot of a WebGL globe fails on driver differences and answers "something changed" when the
  question is "is it blank". Instead: no console errors, nothing fetched off-origin, and the land fill
  colour actually present on the canvas.
- **Interpolation tests** for the two bugs that are invisible in review and obvious on screen.

Two project-specific checks run on every save: one comparing the two dependency manifests, after a
mismatch between them took every route down in production, and one sweeping tracked files for content
that should not be published.

---

## Tradeoffs, and what would come next

- **Postgres over a spatial database.** Nothing here does spatial joins, and requiring PostGIS to draw
  a line would be a dependency bought for nothing.
- **A window of the future over a live stream.** Serverless has no persistent connections. It turned
  out better anyway: jumping to the next event is a scan of an array the client already holds.
- **Whole-plan rejection over partial execution.** When the command layer lands, an invalid step will
  reject the entire plan rather than executing the valid ones, because a plan's steps share referents
  and half-executing an intent leaves a state nobody can explain.
- **Next:** the command layer, an ice-aware constraint on where each kind may exist, and computed mesh
  connectivity in place of a stored neighbour count.

---

## How AI was used in this project

Two separate roles, and they should not be conflated.

**As a development assistant.** A large amount of the implementation here was written by an AI assistant
working to my direction. The architecture, the three rules above, the data model, the validation
approach, the domain choices and every merge decision are mine. Where the model produced something I
disagreed with, it was rewritten or thrown away.

Worth naming a case rather than claiming a clean record: the model concluded from a single build-log
line that this platform reads its dependency list from `pyproject.toml`. It does not, it installs from
`requirements.txt`, and that assumption reached production and took every route down until a missing
driver was traced. The fix was not just the dependency; it was a check that compares the two files on
every save, and moving the driver import so that a database problem can no longer break routes that
never touch the database.

**As a runtime component.** The natural-language command layer is not built yet. When it is, the model
will propose a plan as JSON and never touch state directly: a validator resolves every referenced
entity against live data and rejects anything that does not, on the principle that a hallucinated
value must never reach real state on the model's word alone.

Specifications given to the model live in the repository rather than being paraphrased here, so what it
was actually asked can be read directly.
