"""Database access. One connection helper and the queries the API needs.

⚠️ `prepare_threshold=None` IS NOT OPTIONAL. Neon's pooled endpoint is PgBouncer in
transaction mode, and psycopg3 automatically promotes a statement to a server-side
prepared statement after a few executions. PgBouncer hands the next request a
different backend, which has never seen that statement, so the query fails with
"prepared statement does not exist" only after the same code path has run several
times. That is a bug that passes every test and appears once the app is being used.

The unpooled endpoint is available as DATABASE_URL_UNPOOLED and deliberately not
used: serverless functions open and close connections constantly, which is precisely
what a pooler is for.

Connections are opened per request rather than held in a module-level pool. On a
serverless platform a "pool" spans one invocation, so it buys nothing and hides the
cost of the connection instead of showing it.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import freshness

if TYPE_CHECKING:  # import only for the type annotation on connect()
    import psycopg

# ⚠️ psycopg IS IMPORTED LAZILY, INSIDE connect(). It used to be imported here at module
# scope, and on 2026-08-07 that took the entire deployed API down: the driver was missing
# from requirements.txt, so importing this module raised ModuleNotFoundError, which meant
# `api/index.py` failed to import, which meant EVERY route returned 500 including
# /api/healthz and /api/window, neither of which touches the database.
#
# A database problem should degrade the database endpoints and nothing else. Deferring the
# import to the first connection is what makes that true, and it costs one dict lookup per
# call.


def _database_url() -> str:
    """The pooled Neon URL.

    Reads DATABASE_URL first because that is what the Neon integration sets, then
    POSTGRES_URL as a fallback for the same reason. Raises rather than returning
    None: a missing connection string should fail loudly at the first query, not
    produce an empty asset list that looks like an empty world.
    """
    for key in ("DATABASE_URL", "POSTGRES_URL"):
        url = os.environ.get(key)
        if url:
            return url
    raise RuntimeError(
        "no database URL in the environment (looked for DATABASE_URL, POSTGRES_URL). "
        "Locally: vercel env pull .env.local"
    )


@contextmanager
def connect() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A connection with dict rows and prepared statements disabled.

    ⚠️ THE `dict[str, Any]` IN THAT RETURN TYPE IS LOAD-BEARING. `row_factory=dict_row` is
    what makes every cursor hand back mappings instead of tuples, and the annotation has to
    say so or every `row["name"]` in the codebase type-checks as indexing a tuple with a
    string. That single missing parameter accounted for eleven of the sixteen errors the
    first mypy run produced, in five different files, none of which was actually wrong.
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_database_url(), prepare_threshold=None, row_factory=dict_row) as conn:
        yield conn


def status() -> dict[str, Any]:
    """Is the database reachable, and does it have the world in it?

    Exists because the outage above was diagnosable only from Vercel's runtime logs. One
    curl of /api/healthz should be able to tell "the app is up but the database is not"
    apart from "the app is down", and before this it could not.

    Never raises: a health endpoint that fails when the thing it reports on fails is not a
    health endpoint.
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select count(*) as n from entities")
            row = cur.fetchone()
        # `count(*)` always returns a row, but fetchone() is typed as optional and the
        # compiler is right to insist: the alternative is a TypeError with no explanation
        # inside a health check, which is the worst place to lose the reason.
        return {"reachable": True, "entities": int(row["n"]) if row else 0}
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see the docstring
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def fetch_entities(kind: str | None = None) -> list[dict[str, Any]]:
    """Every entity, or every entity of one kind.

    Returned in a stable order (kind, then id) so that a client diffing two
    responses sees real changes rather than reordering, and so screenshots of the
    same state look the same twice.
    """
    sql = """
        select id, kind, name, lat, lon, alt_m, status, geometry, props,
               last_heard, ais_reporting, created_at, created_by
          from entities
    """
    params: tuple = ()
    if kind:
        sql += " where kind = %s"
        params = (kind,)
    sql += " order by kind, id"

    # 🔑 FRESHNESS IS APPLIED HERE, AT THE ONE READ EVERYTHING GOES THROUGH. `overdue`
    # and `flag` are added to every row, so the map, the status strip, a typed query and
    # the mesh all get the same answer to the same question, computed once against one
    # instant. Answered per caller instead, they drifted apart twice in a day, and even
    # callers sharing one function can disagree across a threshold by a second inside a
    # single response.
    #
    # ⚠️ ANYTHING THAT DEPENDS ON WHETHER AN ASSET IS BEING HEARD FROM MUST RUN AFTER
    # THIS LINE, not before it. `freshness` imports nothing but the standard library, so
    # it is safe to call from here and from the domain without a cycle.
    #
    # 🔴 THE IDLE RESET RUNS BEFORE THE ROWS ARE READ, so a visitor arriving at a quiet
    # moment is served the fresh world rather than the stale one plus a reset that only
    # takes effect on their next request. It shares this cursor: profiling showed a read
    # costing 12.9 seconds, all of it network wait, because the idle check was opening a
    # second connection to ask one cheap question. Imported here rather than at module
    # scope because `lifecycle` imports this module, and one of the two has to be lazy.
    from . import lifecycle

    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        lifecycle.reset_if_idle(cur=cur)
        conn.commit()
        cur.execute(sql, params)
        rows = [freshness.decorate(_serialise(row), now) for row in cur.fetchall()]

    # ⚠️ AND MOTION RUNS AFTER `decorate`, NOT BEFORE IT. `advance` only moves assets we
    # are currently hearing from, so it needs `overdue` to already be on the row. Run it
    # first and everything looks fresh and everything moves, including the assets whose
    # whole story is that they stopped.
    from . import motion

    motion.advance(rows, now)
    return rows


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Make a row JSON-safe without losing type information.

    Timestamps become ISO 8601 strings rather than being dropped or turned into
    epoch numbers, because the client needs to display them and "3 hours ago"
    should be computed from a real instant, not reconstructed from a formatted
    string.
    """
    out = dict(row)
    for key in ("last_heard", "created_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


UPSERT_ENTITY = """
    insert into entities
        (id, kind, name, lat, lon, alt_m, status, geometry, props,
         last_heard, ais_reporting, created_by)
    values
        (%(id)s, %(kind)s, %(name)s, %(lat)s, %(lon)s, %(alt_m)s, %(status)s,
         %(geometry)s, %(props)s, %(last_heard)s, %(ais_reporting)s, %(created_by)s)
    on conflict (id) do update set
        name = excluded.name,
        lat = excluded.lat,
        lon = excluded.lon,
        alt_m = excluded.alt_m,
        status = excluded.status,
        geometry = excluded.geometry,
        props = excluded.props,
        last_heard = excluded.last_heard,
        ais_reporting = excluded.ais_reporting
"""


def entity_params(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        **entity,
        "geometry": json.dumps(entity["geometry"]) if entity.get("geometry") else None,
        "props": json.dumps(entity.get("props") or {}),
    }


def insert_entities(entities: list[dict[str, Any]]) -> None:
    """Insert or update many entities in ONE connection and one transaction.

    🔴 THIS EXISTS BECAUSE THE PER-ROW VERSION MADE A RESET TAKE SEVENTEEN SECONDS. Laying
    the world back down is 76 rows, and calling `insert_entity` for each opened 76 separate
    connections to a database on the other side of a network. Measured at about 220 ms each.

    That cost landed in the worst possible place: the idle reset runs inside a visitor's
    first request, so someone arriving at a quiet moment waited the whole seventeen seconds
    staring at nothing. One connection does the same work in well under a second.
    """
    if not entities:
        return
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(UPSERT_ENTITY, [entity_params(e) for e in entities])
        conn.commit()


def insert_entity(entity: dict[str, Any]) -> None:
    """Insert one entity. Used by the create tools, which genuinely have only one."""
    insert_entities([entity])


def delete_entity(entity_id: str) -> bool:
    """Remove one entity. True if a row went, False if there was nothing to remove.

    🔑 THE AUDIT LOG SURVIVES THIS, AND THAT IS NOT AN ACCIDENT. `events.entity_id` is a
    plain text column with no foreign key, chosen so that deleting an asset cannot erase
    the record of what was done to it. This function is the case that decision was made
    for: after it runs, "what happened to the thing that is no longer here" is still a
    question the log can answer.

    ⚠️ RETURNS WHETHER ANYTHING WAS DELETED rather than succeeding silently. A caller that
    cannot tell "removed" from "was never there" would report both as done, and an
    operator who mistyped a name would be told their asset is gone while it sits on the
    map.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute("delete from entities where id = %s", (entity_id,))
        removed = cur.rowcount > 0
        conn.commit()
        return removed


def log_event(
    *,
    tool: str,
    source: str,
    result: str,
    command_id: str | None = None,
    parent_command_id: str | None = None,
    tier: str | None = None,
    params: dict | None = None,
    detail: str | None = None,
    entity_id: str | None = None,
    latency_ms: int | None = None,
    actor: str = "operator",
) -> int:
    """Append one row to the audit log and return its id.

    Keyword-only on purpose. This is called from every tool, and a positional
    signature with three adjacent text arguments (tool, source, result) is one
    transposition away from an audit log that is quietly wrong about what happened.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into events
                (command_id, parent_command_id, actor, source, tier, tool, params,
                 result, detail, entity_id, latency_ms)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                command_id,
                parent_command_id,
                actor,
                source,
                tier,
                tool,
                json.dumps(params or {}),
                result,
                detail,
                entity_id,
                latency_ms,
            ),
        )
        row = cur.fetchone()
        # 🔑 THE IDLE CLOCK IS RESET FROM HERE, and this is the right place rather than a
        # convenient one. "Has anything happened" has to mean a COMMAND, not a request: the
        # browser polls the map every few seconds, so keying activity off traffic would
        # leave one forgotten tab holding the world open forever and the reset would never
        # fire once. Every command writes to this log, so nothing can act on the world
        # without passing through here.
        cur.execute(
            """
            insert into world_state (id, last_activity) values (1, now())
            on conflict (id) do update set last_activity = now()
            """
        )
        conn.commit()
    if row is None:
        # `returning id` on an insert always yields a row. Raising rather than inventing an
        # id matters here more than anywhere: the caller uses this to thread a command's
        # steps together in the audit log, and a fabricated id would silently split one
        # command into unrelated rows.
        raise RuntimeError("event insert returned no id")
    return int(row["id"])


def fetch_events(
    since_id: int = 0,
    limit: int = 200,
    entity_id: str | None = None,
    command_id: str | None = None,
) -> list[dict[str, Any]]:
    """Audit log, newest last, optionally filtered.

    `since_id` rather than a timestamp cursor: bigserial ids are assigned in
    request order and a client polling for new rows wants "what have I not seen",
    which is an id question. ⚠️ Ids can still COMMIT out of order under
    concurrency, so a client that must not miss a row should overlap its cursor
    slightly rather than trust strict monotonicity.
    """
    sql = "select * from events where id > %s"
    params: list[Any] = [since_id]
    if entity_id:
        sql += " and entity_id = %s"
        params.append(entity_id)
    if command_id:
        sql += " and command_id = %s"
        params.append(command_id)
    sql += " order by id asc limit %s"
    params.append(min(limit, 1000))

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out = []
    for row in rows:
        r = dict(row)
        r["ts"] = r["ts"].isoformat()
        for key in ("command_id", "parent_command_id"):
            if r.get(key) is not None:
                r[key] = str(r[key])
        out.append(r)
    return out


def apply_schema(schema_sql: str) -> None:
    """Run the DDL. Idempotent, because every statement in it is if-not-exists."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(schema_sql)
        conn.commit()
