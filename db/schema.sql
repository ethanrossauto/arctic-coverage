-- Arctic Coverage schema. Two tables, and the shape of them is a design position
-- rather than a default, so the reasoning is here rather than in a wiki nobody reads.
--
-- THE CORE DECISION: one entities table for every kind, not a table per kind.
--
-- ⚠️ Written as "every kind" rather than a number on purpose. This line said "six kinds,
-- not six tables" while the check constraint below permitted ten, because a count in prose
-- does not move when a kind is added. The constraint is the list; this is the argument.
--
-- The kinds differ a lot. A mesh node is a static point with a battery. A Ranger
-- patrol is a route with a position along it. A vessel is a moving contact that may
-- or may not be broadcasting. Three ways to model that:
--
--   * A table per kind. Honest about the differences, and then every query that
--     asks "what is near here" becomes a union across every kind, which is most
--     queries.
--   * One table with every field as a nullable column. One query surface, and
--     twenty mostly-null columns that no constraint can keep honest.
--   * One table, a shared core as real columns, per-kind detail in jsonb.
--
-- The third, because the shared core is genuinely shared: everything has an id, a
-- kind, a name, a position and a status. That is what almost every query filters
-- on, and what the map draws.
--
-- TWO EXCEPTIONS, PROMOTED OUT OF jsonb ON PURPOSE:
--
--   last_heard      when this asset last reported. Applies to nearly every kind and
--                   drives the single most operationally important question there
--                   is: what has gone quiet.
--   ais_reporting   whether a contact is broadcasting its identity. Vessel-only, so
--                   null everywhere else, and promoted anyway because "show me
--                   what is not broadcasting" is the question this whole system
--                   exists to answer.
--
-- Promoting a type-specific field to a column is not schema purity. It is chosen
-- because indexing and query clarity beat purity for the two fields you actually
-- filter on, and left in jsonb they would need a GIN index and a cast at every
-- call site.

create table if not exists entities (
    id           text primary key,
    kind         text        not null,
    name         text        not null,

    -- Position. Nullable because a route-based entity's position is derived from
    -- its geometry and the clock, not stored.
    lat          double precision,
    lon          double precision,
    alt_m        double precision,

    status       text        not null default 'nominal',

    -- GeoJSON LineString for anything with a path: patrol routes, UAS orbits,
    -- vessel tracks. Null for point entities. Stored as jsonb rather than PostGIS
    -- geometry because nothing here does spatial joins, and requiring a PostGIS
    -- extension to draw a line would be a dependency bought for nothing.
    geometry     jsonb,

    -- Per-kind detail. Documented per kind in api/_lib/assets.py.
    props        jsonb       not null default '{}'::jsonb,

    -- See the note above on why these two are columns.
    last_heard   timestamptz,
    ais_reporting boolean,

    created_at   timestamptz not null default now(),
    created_by   text        not null default 'seed',

    constraint entities_kind_check check (
        kind in ('node', 'patrol', 'uas', 'launch_site', 'hydrophone', 'vessel', 'radar', 'marker', 'aircraft', 'ground_party')
    ),
    constraint entities_created_by_check check (
        created_by in ('seed', 'user', 'llm')
    ),
    -- A point entity needs a position; a route entity needs a route. Enforced here
    -- rather than trusted to the seed script, because the failure mode is an
    -- invisible entity, which reads as a rendering bug for an hour before anyone
    -- checks the row.
    constraint entities_has_a_place check (
        (lat is not null and lon is not null) or geometry is not null
    )
);

-- ⚠️ THESE INDEXES DO NOTHING AT THE CURRENT ROW COUNT, and that is stated here
-- rather than glossed over. `explain` on the last_heard filter returns a Seq Scan,
-- because with 50 rows a sequential scan genuinely is cheaper than an index lookup
-- and the planner is right to pick it. They are declared for the SHAPE of the
-- queries, so that the filters the application leans on stay cheap if the table
-- grows by three orders of magnitude. Claiming they make anything faster today
-- would be a claim I checked and found false.
create index if not exists entities_kind_idx        on entities (kind);
create index if not exists entities_last_heard_idx  on entities (last_heard);
create index if not exists entities_ais_idx         on entities (ais_reporting)
    where ais_reporting is not null;
create index if not exists entities_created_by_idx  on entities (created_by);

-- The audit log. Every tool invocation lands here before its effects are visible,
-- which is what makes "the log is complete" a property of the code rather than a
-- promise.
create table if not exists events (
    id                bigserial primary key,
    ts                timestamptz not null default now(),

    -- Groups every step of one utterance or one button press.
    command_id        uuid,
    -- Set when a clarification re-submits a resolved command, so a vague ask and
    -- its resolution read as one interaction instead of two unrelated ones.
    parent_command_id uuid,

    actor             text        not null default 'operator',
    source            text        not null,
    -- Which tier produced the plan. This column is why "the model is only called
    -- when it earns its latency" is a query rather than a claim.
    tier              text,

    tool              text        not null,
    params            jsonb       not null default '{}'::jsonb,
    result            text        not null,
    detail            text,
    latency_ms        integer,

    -- ⚠️ DELIBERATELY NOT A FOREIGN KEY. An audit log that cascades or nulls when
    -- an entity is deleted is an audit log that forgets what you deleted, which is
    -- the opposite of the job. The id is kept as text even after the row it names
    -- is gone.
    entity_id         text,

    constraint events_source_check check (
        source in ('ui_button', 'typed', 'voice', 'system')
    ),
    -- 🔑 'clarify' IS ITS OWN OUTCOME, NOT A FLAVOUR OF 'rejected'. The three original
    -- values answer "did it work", and a clarification answers none of them: the system
    -- understood the request, declined to guess, and asked a question it already knows
    -- every valid answer to. Folded into 'rejected' it would be indistinguishable from a
    -- real refusal, and the two questions worth asking of this log are exactly the ones
    -- that difference decides: how often does the system have to ask, and how often does
    -- it have to say no. A row whose `parent_command_id` points at a 'clarify' row is a
    -- vague request that got resolved, which is a success story living in the same
    -- column as a failure.
    constraint events_result_check check (
        result in ('ok', 'rejected', 'clarify', 'error')
    ),
    constraint events_tier_check check (
        tier is null or tier in ('parser', 'llm')
    )
);

create index if not exists events_entity_idx  on events (entity_id);
create index if not exists events_command_idx on events (command_id);
create index if not exists events_ts_idx      on events (ts desc);
create index if not exists events_tier_idx    on events (tier);

-- --------------------------------------------------------------------------
-- Migrations
-- --------------------------------------------------------------------------
--
-- 🔴 `create table if not exists` IS A NO-OP ON AN EXISTING TABLE, INCLUDING ITS
-- CONSTRAINTS. Editing the `entities_kind_check` list above changes what a FRESH
-- database gets and changes nothing at all about one that already exists. The seed then
-- fails at the first row of the new kind with a check violation, and the schema file
-- that appears to permit it is right there in the repo contradicting the error.
--
-- Found in review before it was hit, which is the only reason it is a comment and not
-- an outage.
--
-- So constraints that change are dropped and re-added explicitly. This is idempotent and
-- safe to run on every seed: `if exists` covers the fresh-database case where the
-- constraint was created by the statement above with the correct definition already.
alter table entities drop constraint if exists entities_kind_check;
alter table entities add constraint entities_kind_check check (
    kind in ('node', 'patrol', 'uas', 'launch_site', 'hydrophone', 'vessel', 'radar', 'marker', 'aircraft', 'ground_party')
);

-- --------------------------------------------------------------------------
-- Spend counters
-- --------------------------------------------------------------------------
--
-- Two integers behind the only endpoint that costs money. See api/_lib/ratelimit.py for
-- why the meter sits in front of tier 2 and nowhere else.
--
-- One row per bucket, where a bucket is an IP and an hour, or the word "global" and a
-- day. The key carries the window, so expiry is "stop reading old rows" rather than a
-- sweeper job, and the whole check is one upsert that returns the new count.
create table if not exists spend_counters (
    bucket     text primary key,
    count      integer     not null default 0,
    updated_at timestamptz not null default now()
);


-- The idle clock, for the demo's own housekeeping. See api/_lib/lifecycle.py.
--
-- 🔑 ONE ROW, ENFORCED BY A CHECK CONSTRAINT rather than by everyone remembering to pass
-- the same id. This is global state about the world, not a per-something counter, and a
-- second row would mean two clocks disagreeing about whether the world is idle. The
-- constraint makes that unrepresentable instead of merely discouraged.
--
-- ⚠️ Two timestamps, not one, and they answer different questions. `last_activity` is when
-- a command last ran and decides whether a reset is DUE. `last_reset` is when the world was
-- last laid back down and decides whether one is ALLOWED yet, which is the floor that stops
-- a bug in the first field turning every page load into a full reseed.
-- ⚠️ `last_reset_cause` is not bookkeeping, it is what the display tells the viewer. A world
-- that changes under someone is alarming when it is unexplained and unremarkable when it is
-- named, and the two causes need different sentences: an idle timeout is something they were
-- warned about, another viewer pressing reset is not.
create table if not exists world_state (
    id               smallint primary key default 1,
    last_activity    timestamptz not null default now(),
    last_reset       timestamptz not null default now(),
    last_reset_cause text        not null default 'seed',
    constraint world_state_single_row check (id = 1)
);

-- 🔴 THE SAME NO-OP TRAP AS THE CONSTRAINT ABOVE, and it bites identically. Adding a column
-- to the statement above changes what a FRESH database gets and changes nothing at all about
-- one that already exists, so the first write naming the new column fails against a schema
-- file that is sitting in the repo appearing to declare it.
alter table world_state add column if not exists last_reset_cause text not null default 'seed';
