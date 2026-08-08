"""The shared world's housekeeping: the idle clock, the reset, and what counts as presence.

Runs with no database, no network and no server. The world_state row is a fixture and the
connection is a double, because what is being tested here is a set of RULES about when the
world may be torn down, and those are decisions rather than queries.

🔑 THE ASSERTION THIS FILE EXISTS FOR is that reading the clock and being present are
different things. Every other test here would still pass if a poll counted as activity, and
the display would then never reset for anybody, because one tab left open on a second
monitor would hold it open forever.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api import index
from api._lib import db, lifecycle


class _FakeCursor:
    def __init__(self, state: dict | None, executed: list[tuple[str, object]]) -> None:
        self._state = state
        self.executed = executed

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def executemany(self, sql: str, rows: object) -> None:
        self.executed.append((" ".join(sql.split()), f"{len(list(rows))} rows"))

    def fetchone(self) -> dict | None:
        return self._state


class _FakeConn:
    def __init__(self, state: dict | None, executed: list[tuple[str, object]]) -> None:
        self._state = state
        self.executed = executed
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._state, self.executed)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def clock(monkeypatch):
    """A world_state row under test control, plus the SQL that ran against it."""
    executed: list[tuple[str, object]] = []
    state = {
        "last_activity": datetime.now(UTC),
        "last_reset": datetime.now(UTC) - timedelta(hours=1),
        "last_reset_cause": "seed",
    }
    monkeypatch.setattr(db, "connect", lambda: _FakeConn(state, executed))
    return state, executed


@pytest.fixture
def logged(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(db, "log_event", lambda **kw: rows.append(kw) or len(rows))
    return rows


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


def test_the_default_window_is_thirty_minutes():
    """Five was far too short for the thing the window actually protects.

    A visitor arriving at any quiet moment triggers the reset on their own first request, so
    the window buys no freshness at all. What it decides is how long somebody can read,
    think or talk before their work is destroyed under them, and five minutes of that is an
    ordinary pause.
    """
    assert lifecycle.IDLE_RESET_MINUTES == 30


def test_status_counts_down_and_names_the_generation(clock):
    state, _ = clock
    state["last_activity"] = datetime.now(UTC) - timedelta(minutes=10)

    s = lifecycle.status()

    assert s["enabled"] is True
    assert s["idle_seconds"] == pytest.approx(600, abs=2)
    # 30 minutes minus 10 elapsed, so 20 to go.
    assert s["seconds_until_reset"] == pytest.approx(1200, abs=2)
    assert s["generation"] == state["last_reset"].isoformat()
    assert s["cause"] == "seed"


def test_status_reports_disabled_rather_than_counting_down_to_nothing(clock, monkeypatch):
    """With the reset off, a display promising "resets after 0 min idle" would be lying.

    The client cannot infer this from a countdown of zero, so it is reported rather than
    derived.
    """
    monkeypatch.setattr(lifecycle, "IDLE_RESET_MINUTES", 0.0)

    s = lifecycle.status()

    assert s["enabled"] is False
    assert s["seconds_until_reset"] is None


# --------------------------------------------------------------------------
# What counts as presence
# --------------------------------------------------------------------------


def test_marking_activity_writes_no_audit_row(clock, logged):
    """🔑 THE DISTINCTION THE WHOLE DESIGN RESTS ON.

    A command refreshes the clock as a side effect of writing its audit row. A pan or a
    zoom must refresh the clock and write NOTHING, because the log records what happened to
    the world and moving a camera did not happen to the world. Routing these through
    `log_event` to save a function would fill the one artifact an evaluator inspects with
    mouse movements.
    """
    _, executed = clock

    lifecycle.mark_activity()

    assert any("world_state" in sql and "last_activity" in sql for sql, _ in executed)
    assert logged == [], "an interaction is not an event in the audit log"


def test_a_reset_is_not_due_while_the_window_has_time_left(clock):
    state, _ = clock
    state["last_activity"] = datetime.now(UTC) - timedelta(minutes=5)

    with db.connect() as conn, conn.cursor() as cur:
        assert lifecycle._is_due(cur, datetime.now(UTC)) is False


def test_a_reset_is_due_once_the_window_has_elapsed(clock):
    state, _ = clock
    state["last_activity"] = datetime.now(UTC) - timedelta(minutes=31)

    with db.connect() as conn, conn.cursor() as cur:
        assert lifecycle._is_due(cur, datetime.now(UTC)) is True


def test_the_floor_stops_a_reset_treading_on_the_last_one(clock):
    """A bug in the activity bookkeeping must not turn every read into a full reseed."""
    state, _ = clock
    state["last_activity"] = datetime.now(UTC) - timedelta(minutes=31)
    state["last_reset"] = datetime.now(UTC) - timedelta(seconds=5)

    with db.connect() as conn, conn.cursor() as cur:
        assert lifecycle._is_due(cur, datetime.now(UTC)) is False


# --------------------------------------------------------------------------
# The reset itself
# --------------------------------------------------------------------------


def test_the_reset_writes_one_row_explaining_its_own_emptiness(clock, logged):
    """An audit log that is simply blank cannot be told from one that was never written.

    The deletion is defensible precisely because the first thing left behind says what
    happened, which turns "there is nothing here" into "nothing has happened since".
    """
    lifecycle._reset_world(seed_rows=[], cause="idle")

    assert len(logged) == 1
    row = logged[0]
    assert row["tool"] == "world_reset"
    assert row["params"]["cause"] == "idle"
    assert "idle window" in row["detail"]


def test_a_manual_reset_says_so_rather_than_blaming_the_clock(clock, logged):
    """The two causes need different sentences: one was announced by a countdown beforehand
    and the other arrived with no warning at all."""
    lifecycle._reset_world(seed_rows=[], cause="manual")

    assert logged[0]["params"]["cause"] == "manual"
    assert "on request" in logged[0]["detail"]


def test_the_cause_is_stored_so_the_display_can_read_it_back(clock, logged):
    _, executed = clock

    lifecycle._reset_world(seed_rows=[], cause="manual")

    stored = [p for sql, p in executed if "last_reset_cause" in sql]
    assert stored and stored[0] == ("manual", "manual")


def test_asking_for_a_reset_too_soon_answers_with_the_wait(clock, logged):
    """A person is watching a button, so the answer has to be something the interface can
    say out loud rather than a bare failure."""
    state, _ = clock
    state["last_reset"] = datetime.now(UTC) - timedelta(seconds=10)

    out = lifecycle.reset_now(seed_rows=[])

    assert out["ok"] is False
    assert out["retry_after_s"] == pytest.approx(50, abs=2)
    assert logged == [], "a refused reset must not write a row saying it happened"


def test_a_permitted_reset_reports_the_new_state(clock, logged):
    state, _ = clock
    state["last_reset"] = datetime.now(UTC) - timedelta(minutes=5)

    out = lifecycle.reset_now(seed_rows=[])

    assert out["ok"] is True
    assert logged and logged[0]["tool"] == "world_reset"


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


def test_the_world_endpoint_serves_the_clock(clock, logged):
    client = TestClient(index.app)

    body = client.get("/api/world").json()

    assert body["enabled"] is True
    assert body["idle_reset_minutes"] == 30
    assert "generation" in body


def test_touching_is_a_post_and_reading_is_a_get(clock, logged):
    """They mean opposite things, so they are different verbs.

    Answering "what is the state" must not change the state, or one open tab holds the
    world alive forever. Saying "a person did something" must.
    """
    client = TestClient(index.app)
    _, executed = clock

    client.get("/api/world")
    reads = sum(1 for sql, _ in executed if "last_activity = now()" in sql)
    assert reads == 0, "polling the clock must never wind it"

    client.post("/api/world/touch")
    writes = sum(1 for sql, _ in executed if "last_activity = now()" in sql)
    assert writes == 1


def test_the_reset_endpoint_refuses_with_429_and_the_seconds_to_wait(clock, logged):
    state, _ = clock
    state["last_reset"] = datetime.now(UTC) - timedelta(seconds=10)
    client = TestClient(index.app)

    res = client.post("/api/reset")

    assert res.status_code == 429
    assert res.json()["retry_after_s"] == pytest.approx(50, abs=2)
    assert res.headers["retry-after"]
