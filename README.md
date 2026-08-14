# Arctic Coverage

An Arctic situational-awareness console with a natural-language interface. You type or speak a
command, and one validated path turns it into a tool call against the asset picture. An audit row is
written before anything on screen changes.

The world is a deployable sensor network across the Northwest Passage chokepoints: 76 assets of
nine kinds, a mesh reachability model, sensors that compute what they can actually detect, position
history you can query, and five years of measured sea ice drawn as context.

Live: **[coverage.skryer.ca](https://coverage.skryer.ca)**

---

## What it does today

- **Typed and spoken commands** answered through one registry of 17 validated tools. Say it the
  declared way and it is instant and free, or say it any other way and the model works it out:
  "show me what is not broadcasting", "fly Daymark 05 to Barrow Strait", "what are we not seeing".
- **A two-tier command path built on a declared command language.** The first tier answers 16
  sentences with no model call at all, exactly one per tool, each opening with a verb no other
  sentence uses, each anchored to the whole utterance, so it either recognises a request completely
  or hands it over. Anything outside the language goes to a language model, which
  proposes a plan as JSON and never touches state directly. The reference card in the console prints
  the language, one canonical sentence per tool, and is rendered from the same table the parser matches
  against.
- **A question instead of a guess.** "Readout on Daymark" names five drones, so the console asks
  which one and offers each as a button carrying the command with it. Answering costs a click rather
  than a retyped sentence, and the question comes from the deterministic tier, so it is immediate and
  free.
- **One sentence, several actions.** "Isolate Daymark 05" expands into three steps: put the camera on
  the asset and select it, resolve what the name matched, and open its detail. The steps refer back to
  what earlier steps resolved, so a plan is a sequence rather than a batch.
- **An audit log** carrying every command: what was said, which tier answered, the plan, each step and
  its outcome. Written before any effect is visible, and readable on screen, where the steps of one
  request stay gathered under it. The transcript in the command bar is one browser's memory and dies
  with the tab; the log is what the server committed. If the two ever disagree, the log is right.
- **A mesh graph** computed from live positions, answering two different questions: who can talk to
  whom, and whose messages can still reach this display.
- **A detection model.** A sensor's payload decides what it can actually see, so the display can
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

A plan can hold several steps, and a later step can refer to what an earlier one resolved instead of
repeating a name the operator typed. "Isolate Daymark 05" resolves the asset once, then filters and
describes whatever that first step found. Sharing a referent is what makes it one sequence rather
than three commands in a row, and it is also why validation is atomic: a plan whose steps depend on
each other cannot sensibly half-run.

### 3. The client store holds domain objects, never map-library shapes

Entities with latitude and longitude, links as endpoint pairs, tracks as arrays of points. No GeoJSON
and no style config in application state. That is what keeps the renderer replaceable.

---

## Data model

Two tables. The shape is a choice, not a default.

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
  are muted for the same reason: measured on the default view, the backhaul badge put six times as much
  ink on screen as the one contact nobody can identify, and the badge is the wrong thing to be loudest.
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

It could have been a radio horizon formula, and an earlier version was. A formula invites a conversation about
Fresnel clearance, fade margin, antenna patterns, terrain masking and auroral absorption, and this
project carries the terrain data for none of them. A number you can defend in one sentence beats a
formula you cannot defend at all. **So the graph is an optimistic upper bound on connectivity, not a
prediction about any particular radio**, and every one of those unmodelled effects makes a real link
worse rather than better.

**Connected means connected to this display.** An asset is reachable when a path exists from it to a
satellite gateway and every hop along the way is one we are currently hearing from. So a relay going
quiet takes its neighbours with it. That is the honest answer: if the only route home runs through a
node that stopped reporting, nothing behind it is reaching us either.

**Freshness follows the same path.** This console's information about an asset is only as current as
the stalest hop on its best route home, so a unit sitting behind a relay that died two days ago was
last heard two days ago, however well its own radio is working. That number is computed from the
graph rather than stamped per asset, and computing it is what stopped the display reporting an asset
as cut off and heard from eight minutes ago in the same frame.

**Three sensor payloads that fail differently.** A node carries one of them. RF reaches 45 km and is
defeated entirely by switching a transmitter off. Electro-optical and infrared reaches 15 km and is
the one that identifies. Magnetic anomaly reaches 4 km and does not care how quiet the target is
being. Separately, the hydrophone barrier across Lancaster Sound reaches 18 km and only hears things
in the water. Mixing them along one shoreline is only worth explaining if
they disagree, and this is where they disagree.

**Sea ice: one claim.** Each date is the sea ice concentration a satellite measured that day, from the
NSIDC Sea Ice Index. Nothing is modelled. The decode validates against figures NSIDC publish
independently, which is a stronger correctness argument than any test written against itself.

> Concentration is the fraction of sea surface covered by ice. **It is not thickness and it says
> nothing about what will bear a load.** A 25 km cell reading 90% says nothing about the particular
> hundred metres under a vehicle.

The grid is resampled to about the source cell size and no finer, because below 25 km there is no
measurement to draw. Smoothing between cells happens at render time and adds no
data.

---

## What the display does not claim

Worth its own section, because the failures this project most wanted to avoid are all overclaims.

- **The mesh graph is optimistic.** See above. It is a planning aid, not a link budget.
- **"Isolate" filters on a kind, not on a single asset.** Naming a kind hides everything else;
  naming one asset moves the camera to it, selects it and opens its detail, but leaves the rest of the
  picture drawn. That asymmetry is a gap rather than a decision, and it is stated here rather than
  papered over, because a bullet above used to describe the single-asset case as filtering.
- **Terrain refusals are trustworthy; terrain approvals are not.** The coastline is a simplified
  polygon set, so within a few kilometres of a shore the data cannot resolve which side a point is on.
  A refusal names a medium and a distance and can be checked. An approval near a coast proves nothing,
  and nothing here claims it does.
- **The undetected unknowns are simulation ground truth, not an observation.** The coverage view can
  reveal contacts whose detection never arrived. **The console could not legitimately know those
  exist**, so they sit behind a control that hides them by default, and the default view asserts only
  what the sensor network actually delivered. The line is whether the detection reached this console,
  not whether some sensor holds the contact: a sensor holding something it cannot report leaves the
  console exactly where it would be if nothing held it, so both cases sit on the same side of the line. What the status
  strip counts is the other case, **detected unknown**: contacts we do hold, that will not say what
  they are.
- **The world is shared, and it resets.** One database, one world, so every visitor is looking at the
  same thing and anyone can move or delete an asset. It returns to the seeded scenario after thirty
  minutes with nobody using it, or when someone presses the reset control. A reset applies to everyone
  currently viewing, not only to whoever triggered it. The display says so on the
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
- **The world resets itself after thirty minutes with nobody using it.** `last_heard` is stored
  absolute, so a seeded world ages until every asset is overdue and "what has gone quiet" has no
  useful answer. Using it means a command or a deliberate act on the display, a pan, a zoom, a
  selection, a layer toggle. A browser polling the map does not count, because one forgotten tab
  would otherwise hold the world open forever.
- **Each ice date is a separate image, fetched when shown.** The alternative was one bundle of every
  date, which cost 1.4 MB before the first frame. A visitor now downloads about 14 KB.
- **Static assets come off a CDN, not through a function.** The basemap is the largest thing this app
  serves, and no request for it should wake a Python process.

---

## Testing

`bash tests/run.sh` runs everything: the Python suite, type checking on both languages, linting on
both, and two project-specific checks. CI runs exactly that script, so a red build is reproducible
locally with one command. The browser suite is opt-in because it needs a database, and it says so
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

Two project-specific checks run with the suite, so CI runs them too: one comparing the two dependency manifests, after a
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
- **Next: hand the model the known names on every call, not just on one route.** There are two routes
  to the second tier. When the first tier produces a plan naming something that does not exist, the
  retry hands the model the failed name and every real one; when the sentence is outside the command
  language, the model gets the sentence alone. So an identical misspelling succeeds or fails depending
  on which route it took, which is arbitrary from where the operator is sitting. Passing the known
  names on every call would fix it. (The first tier's own finding does now travel on both routes: it
  says whether the sentence was near a declared command or nowhere close, and that is shown to you
  while the model is working.) It is not done here for a reason worth stating: the
  transcription prompt is built the same way, from live asset names, and on weak audio it inserted
  names nobody had said. A hint list that dominates is a failure mode this project has already
  measured once, and doing it to the tier that answers commands deserves the same measurement first.
- **Next: editing a placed asset beyond the two flags that change behaviour.** Placing sets the kind,
  the position, whether it is unknown, and whether it carries its own satellite terminal. Nothing else
  is editable, before or after. Those two came first because they are the ones that change what the
  console does rather than what it prints: an unknown contact stops announcing itself, so whether it
  appears at all becomes a question about sensor coverage, and an asset without a terminal is
  reachable only through a neighbour that already is. The rest, a name, a speed, a sensor payload,
  whether it is hostile, are facts the display repeats back, and they want an edit panel on the asset
  banner rather than more controls on the place menu.
- **Next: filtering and searching on those same attributes.** The view menu filters by kind, which is
  the only axis it has, and anything narrower is a typed question today. These two want to arrive
  together, because a field you can set and cannot search for is half a feature.
- **Known limit: the model tier sets one placement flag at a time.** Both are reachable by voice, and
  the deterministic tier sets them together for the kinds it recognises. The model tier cannot,
  because they ride on an existing single-valued enum rather than on two booleans of their own. That
  was not a matter of taste. Adding two plain booleans to the plan schema is the obvious shape, and
  the API answered `400 Schema is too complex` and stopped serving the model tier entirely, while the
  whole suite stayed green, because the provider used in tests never sends a schema at all. Compressing
  them onto the enum costs nothing there and costs exactly this: "place an unknown vessel with a
  backhaul" needs either two commands or the deterministic path.
- **Considered and not built: a tier that writes its own tools at runtime.** When a question needs
  something no tool exposes, the tempting move is to let the model generate the query, or the tool,
  and run it. I decided against it on the principle the rest of the command path is built on: model
  output is untrusted input from an unauthenticated source, and nothing it produces reaches state
  until a validator has resolved every value against live data. Generated code cannot be checked
  that way, because checking it is running it. It would also be solving a problem I did not have.
  Every case that pushed me toward it turned out to be a missing parameter rather than a missing
  tool. "The northernmost asset" is the example: that is a comparison across the whole set rather
  than a filter, and no enumerated parameter will ever express every comparison somebody might ask
  for. It is answered instead by handing the model the world as seven fields per asset and letting
  it reply with ids, which the tools re-read from the database like any other answer. That covers
  the whole class of question and leaves the trust boundary exactly where it was.

---

## How AI was used in this project

AI played two roles here, and they are worth keeping separate.

**As a development assistant.** A large amount of the implementation here was written by an AI
assistant working to my direction. The architecture, the three rules above, the data model, the
validation approach, the domain choices and every merge decision are mine. Where the model produced
something I disagreed with, it was rewritten or thrown away.

Worth naming a case rather than claiming a clean record. The model concluded from a single build-log
line that this platform reads its dependency list from `pyproject.toml`. It does not, it installs from
`requirements.txt`, and that assumption reached production and took every route down until a missing
driver was traced. The fix was not just the dependency. It was adding a check that compares the two files on every
suite run, and moving the driver import so a database problem can no longer break routes that never
touch the database.

**As a runtime component.** The second tier of the command path is `claude-opus-5`. It proposes a plan
as JSON and never touches state: a validator resolves every referenced entity against live data and
rejects anything that does not resolve, on the principle that a hallucinated value must never reach
real state on the model's word alone. The first tier is a declared command language, 16 sentences
matched exactly and printed in full on the reference card, which is faster and cheaper and means the app still works when the model is
unavailable. Measured locally across 88 phrasings, tier 1 answers in a median 1.3 seconds against 7.2
for tier 2.

**Every sentence opens with a verb no other sentence uses**, and that is the rule for whether a tool
exists rather than a style preference. Four commands used to begin with `show`, which told an
operator nothing about which of four things they were about to get. Applying the rule deleted two
tools: one could be given no verb of its own, because it was a filter over another tool's results,
and one had a perfectly good verb attached to a camera move the executor already performs on every
answer. The vocabulary is borrowed rather than invented, so a word is guessable by anyone who has
used a console before: a display is decluttered, a sensor is slewed, aircraft are vectored, equipment
is emplaced, and a deadlined asset is one out of service.

⚠️ **The share answered without a model call fell a long way when that tier became exact, and the
number is worth reading carefully rather than as a regression.** An earlier version matched keywords
anywhere in a sentence and caught 23 of those 88. On a set of 21 phrasings kept from that run it
matched 16, but only **9** of those reached the operator: the other 7 were matched and then escalated
anyway, by a second mechanism that subtracted a two-hundred-word list of words that did not count as
dropped and handed over whatever was left. The 18-sentence language serves **3** of the same 21. Of the
6 it stopped serving, 2 had been answered with the wrong tool ("what is the threat level" resolved to
an asset search for something called "threat level") and 4 were correct answers that now cost a model
call. Both mechanisms are gone, replaced by one that cannot half-match.

**So the metric changed meaning.** It was "how often is an unrehearsed phrasing caught by accident",
and it is now "an operator who reads the card drives every tool at zero cost, and everything else gets
the model". The card is the whole language rather than a sample of it, which is the property that makes
the deterministic tier a control surface instead of a cache: nothing is answerable that is not printed.

**Voice transcription is `gemini-3.5-flash-lite`, and audio leaves the browser to reach it.** The two
model components, Gemini for voice and Claude for text, are the only runtime network calls carrying
your data to a third party. That is worth stating plainly. Everything else is vendored: the basemap,
the ice and the terrain polygons all ship with the app, so a demo cannot fail because somebody else
rate-limited it. The two that do leave are the exception, and pretending otherwise would be the kind
of claim this project keeps deleting.

Model calls are metered per address and in total, and the limits fail closed. Every command, whichever
tier answered it, lands in the audit log.
