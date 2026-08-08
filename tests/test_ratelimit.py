"""The spend guard: the one thing standing between a public URL and an API bill.

🔴 THIS FILE EXISTS BECAUSE THE GUARD HAD NO TESTS AT ALL. Every test that touches the
command endpoint monkeypatches `ratelimit.check` to return allowed, which is correct for
those tests and meant the meter itself was exercised by nothing. The properties below are
each written down as reasoning in `ratelimit.py`, and reasoning in a comment is not a test:
the per-IP-before-global ordering in particular was a deliberate fix for a denial of service
and nothing would have noticed it being reversed.

The counters are a dict rather than Postgres. What is being tested is a set of decisions
about who may spend, not whether an upsert works.
"""
from __future__ import annotations

import pytest

from api._lib import db, ratelimit


class _FakeCursor:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self._row: dict | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        bucket = params[0] if params else ""
        if "insert into spend_counters" in sql:
            self.counts[bucket] = self.counts.get(bucket, 0) + 1
            self._row = {"count": self.counts[bucket]}
        else:
            self._row = {"count": self.counts.get(bucket, 0)} if bucket in self.counts else None

    def fetchone(self) -> dict | None:
        return self._row

    # ⚠️ `status` READS WITH `fetchall` AND `_bump` WITH `fetchone`, so a double that
    # implements only one of them reports a working meter as broken. That is the exact trap
    # `test_command.py` records against the connection pool: a stand-in that cannot answer
    # the question production asks produces a confident, entirely wrong failure.
    def fetchall(self) -> list[dict]:
        return [self._row] if self._row else []


class _FakeConn:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.counts)

    def commit(self) -> None:
        pass

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def counts(monkeypatch) -> dict[str, int]:
    store: dict[str, int] = {}
    monkeypatch.setattr(db, "connect", lambda: _FakeConn(store))
    return store


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("OWNER_IPS", "127.0.0.1, ::1 ,localhost")


# --------------------------------------------------------------------------
# The owner exemption
# --------------------------------------------------------------------------


def test_the_owner_moves_neither_counter(counts, owner):
    """🔑 BOTH COUNTERS, NOT JUST THE PER-IP ONE.

    An owner whose calls still bumped the global counter would spend the public allowance
    during a long working session and rate-limit the demo for its visitors, which is the
    opposite of what an exemption is for. The bug this guards against is invisible from the
    owner's own machine: everything works, and the damage lands on strangers.
    """
    for ip in ("127.0.0.1", "::1", "localhost"):
        v = ratelimit.check(ip)
        assert v.allowed is True
        assert v.scope == "owner"

    assert counts == {}, "an exempt caller must leave no trace in either bucket"


def test_a_stranger_is_not_exempt_by_accident(counts, owner):
    v = ratelimit.check("203.0.113.7")

    assert v.allowed is True
    assert v.scope == "ip"
    assert any(b.startswith("ip:203.0.113.7:") for b in counts)
    assert any(b.startswith("global:") for b in counts)


def test_with_no_owner_configured_nobody_is_exempt(counts, monkeypatch):
    """The deployed environment sets no `OWNER_IPS`, deliberately, so this is the shape
    production actually runs in."""
    monkeypatch.delenv("OWNER_IPS", raising=False)

    v = ratelimit.check("127.0.0.1")

    assert v.scope == "ip"
    assert any(b.startswith("ip:127.0.0.1:") for b in counts)


# --------------------------------------------------------------------------
# The order of the two checks
# --------------------------------------------------------------------------


def test_an_address_over_its_own_cap_does_not_drain_the_shared_one(counts, monkeypatch):
    """🔴 THE DENIAL OF SERVICE THIS ORDERING WAS INTRODUCED TO FIX.

    Bumping the global counter first saves a write when everyone together is already over
    the cap. It also means a single address past its own limit keeps draining the shared
    allowance on every refused call, so one caller can spend the demo's entire day for
    everybody else without ever receiving an answer.

    Nothing but this test would notice the two `_bump` calls being swapped back.
    """
    monkeypatch.delenv("OWNER_IPS", raising=False)
    day_ip = next(iter([f"ip:203.0.113.9:{ratelimit.datetime.now(ratelimit.UTC):%Y-%m-%d}"]))
    counts[day_ip] = ratelimit.PER_IP_DAILY  # already at the cap

    before_global = sum(v for k, v in counts.items() if k.startswith("global:"))
    v = ratelimit.check("203.0.113.9")
    after_global = sum(v2 for k, v2 in counts.items() if k.startswith("global:"))

    assert v.allowed is False
    assert v.scope == "ip"
    assert after_global == before_global, "a refused caller must not spend the shared budget"


def test_a_refused_call_still_counts_against_the_caller(counts, monkeypatch):
    """Otherwise someone sitting at the limit can hammer the endpoint for free and the
    meter never moves."""
    monkeypatch.delenv("OWNER_IPS", raising=False)
    day = f"{ratelimit.datetime.now(ratelimit.UTC):%Y-%m-%d}"
    counts[f"ip:198.51.100.4:{day}"] = ratelimit.PER_IP_DAILY

    ratelimit.check("198.51.100.4")

    assert counts[f"ip:198.51.100.4:{day}"] == ratelimit.PER_IP_DAILY + 1


# --------------------------------------------------------------------------
# The caps themselves
# --------------------------------------------------------------------------


def test_the_shared_cap_is_out_of_reach_of_any_single_caller():
    """⚠️ A RELATIONSHIP, NOT A VALUE, because the values are a spending decision and will
    move again.

    If the global cap were within reach of one address, a single visitor could close the
    demo for everybody, and the safety feature would have become the attack. Five times the
    per-address cap means it takes a coordinated handful of addresses rather than one
    person with a loop.
    """
    assert ratelimit.GLOBAL_DAILY >= ratelimit.PER_IP_DAILY * 5


def test_the_caps_are_reported_as_configured(counts):
    """The health endpoint publishes these, so a change here is visible without a deploy
    note."""
    s = ratelimit.status()

    assert s["global_daily_limit"] == ratelimit.GLOBAL_DAILY
    assert s["per_ip_daily"] == ratelimit.PER_IP_DAILY


# --------------------------------------------------------------------------
# Failing closed
# --------------------------------------------------------------------------


def test_a_meter_that_cannot_read_its_own_storage_refuses(monkeypatch):
    """🔒 A meter that silently passes when its storage is down is not a meter.

    The cost of being wrong in this direction is one refused command with an honest message.
    The cost of being wrong in the other is unbounded.
    """
    monkeypatch.delenv("OWNER_IPS", raising=False)

    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(db, "connect", broken)

    v = ratelimit.check("203.0.113.11")

    assert v.allowed is False
    assert v.scope == "error"
