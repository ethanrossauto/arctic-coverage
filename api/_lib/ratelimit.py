"""Spend guards for the one endpoint that costs money.

WHY THIS EXISTS. The command endpoint is public, and tier 2 calls a paid model. Without a
meter, one script pointed at this URL turns a demo into a bill, and the failure is silent
until someone reads an invoice.

🔑 ONLY TIER 2 IS METERED, AND THAT DISTINCTION IS THE WHOLE DESIGN. Every deterministic
command is free: no network call, no tokens, no money. Counting those against a cap would
mean a visitor typing the example commands gets throttled part-way through a first look,
which is the worst possible moment to say no. So the meter sits immediately
before the model call and nowhere else.

TWO CAPS, BECAUSE THEY STOP DIFFERENT THINGS:

  * PER IP, PER DAY. Stops one person, or one loop, from running up a total on their own.
    A day rather than an hour: the thing being defended against is a script grinding away,
    and an hourly window just lets the same script come back twenty-four times.
  * GLOBAL, PER DAY. Stops everyone at once. A per-IP limit alone is no defence against a
    handful of addresses, and this is a portfolio demo rather than a service with users to
    keep happy.

⚠️ THE OWNER EXEMPTION SKIPS BOTH COUNTERS, not just the per-IP one. An owner whose calls
still moved the global counter would spend the public allowance during a long working
session and rate-limit the demo for everyone else.

🔒 IT FAILS CLOSED. If the counter cannot be read or written, the model call is refused.
A meter that silently passes when its own storage is down is not a meter, and the cost of
being wrong in that direction is unbounded while the cost of being wrong in the other is
one refused command with an honest message.

⚠️ THE COUNTERS LIVE IN POSTGRES, NOT REDIS. The obvious port from the other project's
rate limiter assumed Upstash, which this stack deliberately does not have. A single
upsert-and-return is atomic in Postgres, which is all this needs, and it avoids taking a
second datastore for two integers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db

# Calls per IP per DAY. A day rather than an hour because the thing being protected
# against is a script grinding away, and an hourly window lets the same script come back
# twenty-four times.
PER_IP_DAILY = 50

# Calls across everyone per day. The real backstop: a per-IP limit is no defence at all
# against a handful of addresses.
GLOBAL_DAILY = 300


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    scope: str = ""
    used: int = 0
    limit: int = 0


def _owner_ips() -> set[str]:
    """Addresses exempt from the caps.

    🔑 THIS EXISTS FOR ONE CONCRETE REASON: recording the demo video. Hitting a rate limit
    mid-take, on the machine that owns the project, would be a self-inflicted wound at the
    worst moment. Set `OWNER_IPS` to a comma-separated list.
    """
    raw = os.environ.get("OWNER_IPS", "")
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def _bump(bucket: str, limit: int) -> tuple[bool, int]:
    """Increment a counter and say whether it is still under its limit.

    One statement, so the read and the write cannot interleave with another request. The
    returned count is post-increment, which is what makes the comparison correct under
    concurrency: two simultaneous callers at the limit get different numbers back and only
    one of them is allowed through.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into spend_counters (bucket, count) values (%s, 1)
            on conflict (bucket) do update set
                count = spend_counters.count + 1,
                updated_at = now()
            returning count
            """,
            (bucket,),
        )
        row = cur.fetchone()
        conn.commit()
    used = int(row["count"])
    return used <= limit, used


def check(client_ip: str | None) -> Verdict:
    """May this request make a model call?

    Called immediately before tier 2 and nowhere else. Increments as it checks, so a
    refused call still counts: otherwise a caller sitting at the limit could hammer the
    endpoint for free and the meter would never move.
    """
    ip = (client_ip or "unknown").strip()

    # 🔑 SHORT-CIRCUITS BEFORE EITHER BUMP, deliberately. The owner is exempt from BOTH
    # counts, which means those requests must not move the global counter either, or a long
    # working session would quietly eat the public daily allowance and the demo would be
    # rate-limited for visitors by the person who built it.
    if ip in _owner_ips():
        return Verdict(allowed=True, scope="owner", reason="owner address, exempt from both counts")

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")

    try:
        # Global first. If everyone together is over the daily cap there is no reason to
        # spend a write on the per-IP counter.
        ok_global, used_global = _bump(f"global:{day}", GLOBAL_DAILY)
        if not ok_global:
            return Verdict(
                allowed=False,
                scope="global",
                used=used_global,
                limit=GLOBAL_DAILY,
                reason=(
                    f"this demo has made {GLOBAL_DAILY} model calls today, which is its daily cap. "
                    "Every deterministic command still works: try \"mesh status\", "
                    "\"what is not broadcasting\", or \"show me the drones\""
                ),
            )

        ok_ip, used_ip = _bump(f"ip:{ip}:{day}", PER_IP_DAILY)
        if not ok_ip:
            return Verdict(
                allowed=False,
                scope="ip",
                used=used_ip,
                limit=PER_IP_DAILY,
                reason=(
                    f"you have used {PER_IP_DAILY} model calls today, which is the per-address "
                    "cap. The deterministic commands are unmetered and still work"
                ),
            )

        return Verdict(allowed=True, scope="ip", used=used_ip, limit=PER_IP_DAILY)

    except Exception as exc:  # noqa: BLE001 - see the fail-closed note in the module docstring
        return Verdict(
            allowed=False,
            scope="error",
            reason=f"the spend meter is unavailable, so the model call was refused ({type(exc).__name__})",
        )


def origin_allowed(origin: str | None) -> bool:
    """Is this request coming from somewhere we serve?

    ⚠️ SCOPED TO THE TIER 2 PATH ONLY, deliberately. Anyone curling `/api/command` with
    a deterministic command should get a real answer, not a 403: the guards exist to
    protect spend, not to make the API hostile to someone poking at it. An origin check on
    the free path would do nothing for cost and quite a lot of damage to the demo.

    Absent origin (curl, a server-side call) is allowed. This is a filter against a page on
    someone else's domain quietly driving up a bill, not an authentication scheme, and
    pretending otherwise would be the more dangerous mistake.
    """
    allowed = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if not allowed or not origin:
        return True
    return any(origin.rstrip("/") == a.strip().rstrip("/") for a in allowed.split(","))


def status() -> dict:
    """Current counter values, for the health endpoint and for the README's honesty."""
    now = datetime.now(timezone.utc)
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select bucket, count from spend_counters where bucket in (%s, %s)",
                (f"global:{now:%Y-%m-%d}", f"global:{now:%Y-%m-%d}"),
            )
            rows = cur.fetchall()
        used = rows[0]["count"] if rows else 0
        return {"global_today": used, "global_daily_limit": GLOBAL_DAILY, "per_ip_daily": PER_IP_DAILY}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}
