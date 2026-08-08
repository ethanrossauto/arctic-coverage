# Arctic Coverage

An Arctic situational-awareness console with a natural-language interface. You type or speak a
command, and one validated path turns it into a tool call against the asset picture, with an audit
row written before anything on screen changes.

The world is a deployable sensor network across the Northwest Passage chokepoints: 76 assets across
nine kinds, a mesh reachability model, sensors that compute what they can actually detect, position
history you can query, and five years of measured sea ice drawn as context.

Live: **[coverage.skryer.ca](https://coverage.skryer.ca)**

---

## What it does today

- **Typed and spoken commands** answered through one registry of 15 validated tools. "Show me what is
  not broadcasting", "fly Daymark 05 to Barrow Strait", "where has the Resolute patrol been",
  "what are we not seeing".
- **A two-tier command path.** A deterministic parser answers roughly thirty phrasings with no model
  call at all. Anything it does not recognise goes to a language model, which proposes a plan as JSON and
  never touches state directly.
- **An audit log** carrying every command: what was said, which tier answered, the plan, each step and
  its outcome. Written before any effect is visible, and readable on screen, where the steps of one
  request stay gathered under the request that caused them. The panel is deliberate about what it is:
  the transcript in the command bar is one browser's memory and dies with the tab, while the log is
  what the server committed. If the two ever disagree, the log is right.
- **A mesh graph** computed from live positions, answering two different questions: who can talk to
  whom, and whose messages can still reach this display.
- **A detection model**, so a sensor's payload decides what it can actually see and the display can
  distinguish a quiet ocean from an ocean it cannot hear.
- **Measured sea ice**, 55 dates over five years, drawn as visual context.

---

## Quickstart

```bash
# back end
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e ".[dev]"
.venv/bin/python scripts/seed_db.py          # applies the schema and seeds the world
.venv/bin/uvicorn api.index:app --port 8000

# front end, in a second terminal
npm install
npm run dev

# everything CI runs
bash tests/run.sh
```

Needs a Postgres URL in `DATABASE_URL`. The command layer's second tier needs an API key; without one
it reports itself unavailable and the deterministic parser keeps working.

---

## Architecture

```
React + MapLibre                      Python (ASGI, one FastAPI app)
  globe, panels, command bar  --POST--> /api/command     parse or plan, validate, execute, log
  microphone                  --POST--> /api/transcribe  audio to text, then the same path
                              --GET---> /api/entities    the world, with freshness and mesh flags
                              --GET---> /api/mesh        the link graph and how it was decided
                              --GET---> /api/events      the audit log
                                            |
                                        Postgres
```

Three rules hold this together, and each exists because breaking it caused a real problem.

### 1. The front end never computes domain geometry

Link ranges, reachability, detection, terrain constraints and position history all happen in Python.
The browser draws what it is handed.

The word *domain* is load-bearing. The front end unavoidably computes **camera** geometry, and it
warps the ice grid into the projection the renderer uses. Neither is domain geometry. The line is
worth holding because it keeps one source of truth for anything a person might act on.

### 2. Buttons and language drive one registry through one executor

Every capability is a named, validated function. A button calls it, the parser calls it, and the
model calls it. Nothing is implemented twice, and because the executor writes an audit row before any
effect is visible, the log is complete by construction rather than by discipline.

### 3. The client store holds domain objects, never map-library shapes

Entities with latitude and longitude, links as endpoint pairs, tracks as arrays of points. No GeoJSON
and no style config in application state. That is what keeps the renderer replaceable.

---

## Data model

Two tables. The shape is a position, not a default.

**One `entities` table for every kind, not a table per kind.** The kinds differ a lot, but the shared
core (id, kind, name, position, status) is what almost every query filters on and what the map draws.
A table per kind turns "what is near here" into a nine-way union, and that is most queries. Per-kind
detail lives in `props jsonb`.

**Two type-specific fields are promoted to real columns anyway.** `last_heard` and `ais_reporting`
drive the questions the system is really for: what has gone quiet, and what is not broadcasting. Left
in `jsonb` they would need a cast at every call site to be filterable. That trades schema purity for
indexability, and for exactly two fields, purity loses.

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

**There is no position-history table, and that is the more interesting decision.** One was planned,
with the storage arithmetic worked out. Then motion became derived: a route-based asset's position is a
pure function of its stored row and the clock, so any past instant can be computed exactly rather than
recalled. At that point the table stopped being a record and became a cache of arithmetic, and one that
could disagree with the arithmetic it cached. What it would have cost is now simply absent: a backfill,
a gap filler, a retention policy, a prune job, and the possibility of a hole in the history.

---

## Reading the display

The console is meant to be read in one glance by someone who already knows the region, so the visual
grammar is deliberate and narrow.

- **Status beats kind in the paint order.** "This one is in trouble" has to win over "this one is a
  node", so status drives the outline and opacity while kind drives the fill.
- **Grey means we are not hearing from it.** A grey asset is also a stopped asset: it is drawn where it
  last reported and it does not move, because animating it onward would be the display inventing a
  position.
- **One thing on screen is allowed to be red**: a contact that is not broadcasting. Everything else
  lives in a green-to-blue family, so the eye goes to the anomaly without being told to. Role markings
  are muted for the same reason: the backhaul badge was measured putting six times as much ink on the
  default view as the one contact nobody can identify, which is the wrong thing to be loudest.
- **Existing radar sites are desaturated** and sit behind the deployable layer. They are infrastructure
  to work alongside, not owned assets, and they should read as background.
- **A track drawn dashed was inferred, not reported.** A contact held only by a sensor gets a dashed
  line, because the difference between "it told us where it was" and "we worked out where it was" is
  the entire point.
- **A line on the map means somebody asked a question.** Seeded routes are not drawn. History is a
  query, so a trail appears when it is requested and clears when it is not.
- **No basemap tiles at all.** Land, the graticule and the Arctic Circle are local files. Over the
  Arctic a street-level basemap shows almost nothing, and nothing external can fail during a demo.

---

## Domain notes

**Why the assets sit where they sit.** The mesh clusters on the Northwest Passage chokepoints, because
a sensor is worth what passes it. The hydrophones form one barrier across Lancaster Sound, the eastern
gate everything arriving from the Atlantic has to pass. The drones are based at the real northern
airfields. Scattering them at random would produce the same screenshot and collapse under one question
from someone who knows the region.

**The mesh model is a lookup, and that is deliberate.** Two assets are linked when the distance between
them is inside the range for their pair of states: 25 km ground to ground, 50 km ground to air, 100 km
air to air. Everything is on the ground except a drone in flight. That is the whole rule.

It could have been a radio horizon formula, and it was one. A formula invites a conversation about
Fresnel clearance, fade margin, antenna patterns, terrain masking and auroral absorption, and this
project carries the terrain data for none of them. A number you can defend in one sentence beats a
formula you cannot defend at all. **So the graph is an optimistic upper bound on connectivity, not a
prediction about any particular radio**, and every one of those unmodelled effects makes a real link
worse rather than better.

**Connected means connected to this display.** An asset is reachable when a path exists from it to a
satellite gateway and every hop along the way is one we are currently hearing from. So a relay going
quiet takes its neighbours with it. That is the honest answer: if the only route home runs through a
node that stopped reporting, nothing behind it is reaching us either.

**Three sensor types that fail differently.** RF reaches 45 km and is defeated entirely by switching a
transmitter off. Electro-optical and infrared reaches 15 km and is the one that identifies. Magnetic
anomaly reaches 4 km and does not care how quiet the target is being. The acoustic barrier reaches
18 km and only hears things in the water. Mixing them along one shoreline is only worth explaining if
they disagree, and this is where they disagree.

**Sea ice: one claim.** Each date is the sea ice concentration a satellite measured that day, from the
NSIDC Sea Ice Index. Nothing is modelled. The decode validates against figures NSIDC publish
independently, which is a stronger correctness argument than any test written against itself.

> Concentration is the fraction of sea surface covered by ice. **It is not thickness and it says
> nothing about what will bear a load.** A 25 km cell reading 90% says nothing about the particular
> hundred metres under a vehicle.

The grid is resampled to about the source cell size and no finer, because below 25 km there is no
measurement to draw. Smoothing between cells happens at render time and is a visual treatment, not
data.

---

## What the display does not claim

Worth its own section, because the failures this project most wanted to avoid are all overclaims.

- **The mesh graph is optimistic.** See above. It is a planning aid, not a link budget.
- **Terrain refusals are trustworthy; terrain approvals are not.** The coastline is a simplified
  polygon set, so within a few kilometres of a shore the data cannot resolve which side a point is on.
  A refusal names a medium and a distance and can be checked. An approval near a coast proves nothing,
  and nothing here claims it does.
- **The undetected unknowns are simulation ground truth, not an observation.** The coverage view can
  reveal contacts whose detection never arrived. **The console could not legitimately know those
  exist**, so they sit behind a control that hides them by default, and the default view asserts only
  what the sensor network actually delivered. The line is whether the detection reached this console,
  not whether some sensor holds the contact: a sensor holding something it cannot report leaves the
  console exactly where nothing holding it would, so both sit on the same side of it. What the status
  strip counts is the other case, **detected unknown**: contacts we do hold, that will not say what
  they are.
- **The world is shared, and it resets.** One database, one world, so every visitor is looking at the
  same thing and anyone can move or delete an asset. It returns to the seeded scenario after thirty
  minutes with nobody using it, or when someone presses the reset control, and a reset applies to
  everyone currently viewing rather than only to whoever triggered it. The display says so on the
  status strip, counts down before it happens, and says afterwards that it did. **This is disclosed
  rather than prevented**, because giving each visitor a private world would mean threading a session
  through every entity and every audit row, which is a different application from this one.
- **Position history is reconstructed, not recorded.** Positions come from a model of how each asset
  travels. For seeded assets on known routes those are the same thing by construction. For anything a
  person has moved by hand they are not.
- **An accessibility pass is a floor, not a certificate.** The browser suite fails on any serious or
  critical WCAG 2.1 AA violation, which catches perhaps a third to a half of real issues. It cannot
  tell whether the map is usable by keyboard alone.

---

## Performance

- **Freshness is computed once per request.** `overdue` and the status flag are attached at the single
  read everything goes through, so the map, the status strip and the mesh cannot disagree by a second
  across a threshold inside one response. Answered per caller instead, they drifted apart twice in one
  day.
- **The world resets itself after five minutes with no command.** `last_heard` is stored absolute, so a
  seeded world ages until every asset is overdue and "what has gone quiet" has no useful answer.
  Activity means a command, not a request, because a browser polling the map would otherwise hold the
  world open forever.
- **Each ice date is a separate image, fetched when shown.** The alternative was one bundle of every
  date, which cost 1.4 MB before the first frame. A visitor now downloads about 14 KB.
- **Static assets come off a CDN, not through a function.** The basemap is the largest thing this app
  serves, and no request for it should wake a Python process.

---

## Testing

`bash tests/run.sh` runs everything, and CI runs exactly that script so a red build is reproducible
locally with one command: the Python suite, type checking on both languages, linting on both, and two
project-specific checks. The browser suite is opt-in because it needs a database, and it says so
rather than skipping quietly.

- **The ice tests check against the outside world, not against themselves.** Seasonal extent figures
  are asserted against NSIDC's published range, because a projection error moves ice onto land and a
  scaling error moves the total. They also decode the shipped images and assert the encoding, so a file
  no renderer could read fails here.
- **Browser tests answer one question: does the page work.** No golden images. A reference screenshot
  of a WebGL globe fails on driver differences and answers "something changed" when the question is "is
  it blank". Instead: no console errors, nothing fetched off-origin, and the land fill actually present
  on the canvas.
- **The mesh tests pin the behaviour that is easy to get subtly wrong**, including that a gateway going
  quiet greys its whole cluster while those assets stay linked to each other, and that a drone bridges
  a patrol to its base only while airborne.

Two project-specific checks run on every save: one comparing the two dependency manifests, after a
mismatch between them took every route down in production, and one sweeping tracked files for content
that should not be published.

---

## Tradeoffs, and what would come next

- **Postgres over a spatial database.** Nothing here does spatial joins, and requiring PostGIS to draw
  a line would be a dependency bought for nothing.
- **Validation is atomic; execution is fail-fast.** Any invalid step rejects the whole plan and nothing
  runs, with every reason reported at once, because a plan's steps share referents and half-executing
  an intent leaves a state nobody can explain. Once validated, steps run in order and the first refusal
  stops the rest. **There is no rollback**, and what has already committed stays committed.
- **A lookup over a formula, everywhere it came up.** The mesh model, the detection ranges and the
  camera zoom ladder are all tables rather than derivations. Each is defensible in a sentence.
- **Next:** Canadian Ice Service charts would give measured thickness bands rather than concentration
  alone, which is the one thing the ice layer cannot currently support. The research is done and the
  scheduling call was deliberate: the finding that matters is that the field naming a polygon's ice
  stage reports the **thickest** stage present, so reading it as "the thickness here" would
  systematically overstate how safe the ice is.
- **Next: close the loop between the two tiers.** The deterministic parser works out which of your
  words it could not place, and that finding is shown to you and withheld from the model. There are
  two routes to the second tier and only one of them carries anything from the first: when the parser
  produces a plan naming something that does not exist, the retry hands the model the failed name and
  every real one. When the parser cannot place enough of the sentence to try, the model gets the
  sentence alone. So an identical misspelling succeeds or fails depending on whether the parser
  guessed first, which is arbitrary from where the operator is sitting. Passing the parser's trace and
  the known names on every call would fix it, and is not done here for a reason worth stating: the
  transcription prompt is built the same way, from live asset names, and on weak audio it inserted
  names nobody had said. A hint list that dominates is a failure mode this project has already
  measured once, and doing it to the tier that answers commands deserves the same measurement first.

---

## How AI was used in this project

Two separate roles, and they should not be conflated.

**As a development assistant.** A large amount of the implementation here was written by an AI
assistant working to my direction. The architecture, the three rules above, the data model, the
validation approach, the domain choices and every merge decision are mine. Where the model produced
something I disagreed with, it was rewritten or thrown away.

Worth naming a case rather than claiming a clean record. The model concluded from a single build-log
line that this platform reads its dependency list from `pyproject.toml`. It does not, it installs from
`requirements.txt`, and that assumption reached production and took every route down until a missing
driver was traced. The fix was not just the dependency. It was a check comparing the two files on every
save, and moving the driver import so a database problem can no longer break routes that never touch
the database.

**As a runtime component.** The second tier of the command path is `claude-opus-5`. It proposes a plan
as JSON and never touches state: a validator resolves every referenced entity against live data and
rejects anything that does not resolve, on the principle that a hallucinated value must never reach
real state on the model's word alone. The first tier is a deterministic parser that answers common
phrasings with no model call, which is faster and cheaper and also means the app still works when the
model is unavailable.

**Voice transcription is `gemini-3.5-flash-lite`, and audio leaves the browser to reach it.** That is
worth stating plainly, because nothing else in this application makes a runtime network call. The
basemap, the ice and the terrain polygons are all vendored, so a demo cannot fail because a third party
rate-limited it. Speech is the exception, and pretending otherwise would be the kind of claim this
project keeps deleting.

Model calls are metered per address and in total, and the limits fail closed. Every command, whichever
tier answered it, lands in the audit log.
