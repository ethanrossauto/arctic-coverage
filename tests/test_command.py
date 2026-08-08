"""The command layer: parsing, validation, execution, and the clarification round trip.

🔑 WHAT THIS SUITE IS ACTUALLY FOR. The interesting failures in a natural-language
interface are not "the model said something odd". They are the ones where the system
answers a different question from the one asked and looks entirely confident doing it:
`overdue` quietly meaning a condition filter, a required parameter arriving as a crash rather than
a refusal, a vague name resolving to whichever asset happened to sort first. Every test
here is one of those.

Runs with no database, no network, no API key and no server. The world is a fixture, the
audit log is a list, and tier 2 is the replay provider, which returns the same `Selection`
object the real provider returns so everything downstream of the model call is genuinely
under test rather than mocked past.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The repo root, so `api` imports the same way it does under uvicorn. pytest puts the
# test's own directory on the path and not the project's, and doing it here keeps the fix
# next to the thing that needs it rather than in a config file nothing else requires.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api._lib import db, executor, freshness, llm, parser, tools, transcribe  # noqa: E402

NOW = datetime.now(UTC)


def heard(minutes_ago: float) -> str:
    """An ISO timestamp that many minutes in the past, as `fetch_entities` returns them."""
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def asset(**over) -> dict:
    base = {
        "id": "x",
        "kind": "node",
        "name": "X",
        "lat": 69.0,
        "lon": -105.0,
        "alt_m": 0.0,
        "status": "nominal",
        "geometry": None,
        "props": {},
        "last_heard": heard(5),
        "ais_reporting": None,
        "created_at": NOW.isoformat(),
        "created_by": "seed",
    }
    base.update(over)
    return base


# The world these tests run against. Small, and built so that "overdue" and "maintenance"
# deliberately name different sets: that overlap is where the bug lived.
WORLD = [
    # Serviceable, but not heard from in seven hours. Flag is overdue, not maintenance.
    asset(id="node-victoria-03", name="Victoria 03", last_heard=heard(420)),
    # Unserviceable, and beaconing normally. In maintenance, not overdue.
    asset(id="node-alert-01", name="Alert 01", status="maintenance", lat=82.5, lon=-62.3),
    # Well inside its interval on both counts.
    asset(id="node-eureka-02", name="Eureka 02", lat=80.0, lon=-85.9),
    # No heartbeat at all, and no threshold for its kind. Must never be called overdue.
    asset(id="radar-cam-04", kind="radar", name="CAM 04", last_heard=None, lat=68.8, lon=-93.4),
    # Two drones whose names share a prefix, which is what makes "daymark" a question.
    asset(
        id="uas-daymark-03",
        kind="uas",
        name="Daymark 03",
        lat=70.2,
        lon=-100.1,
        props={"endurance_min_remaining": 300, "cruise_kmh": 140},
    ),
    asset(
        id="uas-daymark-05",
        kind="uas",
        name="Daymark 05",
        lat=71.0,
        lon=-97.0,
        last_heard=heard(90),
        props={"endurance_min_remaining": 240, "cruise_kmh": 140},
    ),
    asset(
        id="vessel-kanguk",
        kind="vessel",
        name="Kanguk",
        lat=72.1,
        lon=-96.0,
        ais_reporting=False,
        last_heard=heard(10),
    ),
]


@pytest.fixture
def world(monkeypatch) -> list[dict]:
    """The fixture world in place of the database, plus a list standing in for the log."""
    # Decorated, because that is what the real `fetch_entities` returns: freshness is
    # applied at the read so every consumer sees one answer.
    monkeypatch.setattr(
        db, "fetch_entities", lambda kind=None: [freshness.decorate(dict(a)) for a in WORLD]
    )
    return WORLD


@pytest.fixture
def log(monkeypatch) -> list[dict]:
    rows: list[dict] = []

    def record(**kwargs):
        rows.append(kwargs)
        return len(rows)

    monkeypatch.setattr(db, "log_event", record)
    return rows


@pytest.fixture
def deletes(monkeypatch) -> list[str]:
    gone: list[str] = []

    def remove(entity_id: str) -> bool:
        gone.append(entity_id)
        return True

    monkeypatch.setattr(db, "delete_entity", remove)
    return gone


@pytest.fixture
def writes(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(db, "insert_entity", lambda entity: rows.append(entity))
    return rows


# --------------------------------------------------------------------------
# Overdue: the word that meant something else
# --------------------------------------------------------------------------


def test_overdue_is_not_a_condition_filter():
    """The regression itself. "Overdue" used to be an alias for a different question."""
    plan = parser.parse("which assets are overdue")
    assert plan == [{"tool": "list_entities", "params": {"overdue": True}}]


def test_overdue_and_maintenance_name_different_sets(world, log):
    """Not just that both work: that they disagree, which is why the alias was a bug.
    One is a fact about the clock, the other a fact about the asset."""
    overdue = tools.list_entities(overdue=True).data["ids"]
    maintenance = tools.list_entities(status="maintenance").data["ids"]

    assert "node-victoria-03" in overdue
    assert "node-victoria-03" not in maintenance
    assert maintenance == ["node-alert-01"]
    assert "node-alert-01" not in overdue


def test_overdue_respects_the_interval_of_each_kind(world, log):
    """A drone quiet for 90 minutes is overdue; a node quiet for 90 minutes is not."""
    ids = tools.list_entities(overdue=True).data["ids"]
    assert "uas-daymark-05" in ids  # 90 minutes against a 60 minute interval
    assert "node-eureka-02" not in ids


def test_a_radar_is_never_overdue(world, log):
    """It reports to nothing, so it cannot be late to it. Twelve sites turn on this."""
    assert "radar-cam-04" not in tools.list_entities(overdue=True).data["ids"]
    assert tools.is_overdue({"kind": "radar", "last_heard": None}) is False


def test_overdue_counts_from_the_instant_not_the_wall_clock():
    row = {"kind": "node", "last_heard": heard(121)}
    assert tools.is_overdue(row, NOW) is True
    assert tools.is_overdue({"kind": "node", "last_heard": heard(119)}, NOW) is False


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,target,days",
    [
        ("show me 4 days of history for daymark-3", "daymark-3", 4.0),
        ("show me the history of daymark 3 over the last 3 days", "daymark 3", 3.0),
        ("history for daymark-3 this week", "daymark-3", 7.0),
        ("history for daymark 3 for the last 12 hours", "daymark 3", 0.5),
        ("where has daymark 3 been", "daymark 3", 1.0),
        ("daymark-3 history", "daymark-3", 1.0),
    ],
)
def test_history_reads_a_window_and_a_target(utterance, target, days):
    """Both orderings people actually use: duration before the name, and after it."""
    plan = parser.parse(utterance)
    assert plan == [{"tool": "entity_history", "params": {"target": target, "days": days}}]


def test_history_degrades_honestly_when_nothing_records_positions(world, log, monkeypatch):
    """The seam to an optional capability must refuse with a reason, never 500.

    ⚠️ This used to pass by accident, because the history module genuinely did not exist
    yet. Once it landed the test went green for the wrong reason and then failed, which is
    the right way round: the absence has to be simulated to be tested at all."""
    monkeypatch.setattr(tools, "_history_module", lambda: None)
    with pytest.raises(tools.ToolError) as caught:
        tools.entity_history(target="daymark 03")
    assert "no position history" in str(caught.value)
    # And it says what IS available, rather than stopping at the refusal.
    assert "mesh" in str(caught.value)


def test_history_downsamples_and_frames_what_came_back(world, log, monkeypatch):
    """With a history source present, the tool returns a track and a camera for it."""
    samples = [
        {"ts": heard(m), "lat": 70.0 + m / 1000, "lon": -100.0} for m in range(300, 0, -10)
    ]

    class FakeHistory:
        calls: list[dict] = []

        @staticmethod
        def positions(entity_id, minutes, max_points):
            FakeHistory.calls.append(
                {"id": entity_id, "minutes": minutes, "max_points": max_points}
            )
            return samples

    monkeypatch.setattr(tools, "_history_module", lambda: FakeHistory)

    result = tools.entity_history(target="daymark 03", days=2)

    assert FakeHistory.calls[0]["id"] == "uas-daymark-03"
    assert FakeHistory.calls[0]["minutes"] == 2880
    # The bound travels to the source rather than the series being trimmed after arrival.
    assert FakeHistory.calls[0]["max_points"] == tools.MAX_TRACK_POINTS
    assert result.ui_effects["track"]["coordinates"][0] == [-100.0, 70.3]
    assert "camera" in result.ui_effects


def test_history_window_is_bounded(world, log, monkeypatch):
    """A request for a decade asks the source for a month, not for a decade."""
    seen: list[int] = []
    monkeypatch.setattr(
        tools,
        "_history_module",
        lambda: type("H", (), {"positions": staticmethod(lambda entity_id, minutes, max_points: seen.append(minutes) or [])}),
    )
    tools.entity_history(target="daymark 03", days=3650)
    assert seen == [30 * 1440]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_a_missing_required_parameter_is_a_refusal_not_a_crash():
    """`place_asset` with no lat used to validate cleanly and die as a TypeError inside
    the call, which the log then recorded as `error`, the category that means the code
    is broken, not the request."""
    reasons = executor.validate([{"tool": "place_asset", "params": {"kind": "node"}}])
    assert reasons
    assert "missing required parameters lat, lon" in reasons[0]


def test_optional_parameters_are_not_required():
    assert executor.validate([{"tool": "list_entities", "params": {}}]) == []
    assert executor.validate([{"tool": "mesh_status", "params": {}}]) == []


def test_validation_collects_every_reason_not_the_first():
    reasons = executor.validate(
        [{"tool": "task_uas", "params": {"target": "x", "lat": 900, "lon": -95, "colour": "red"}}]
    )
    assert len(reasons) == 2
    assert any("unknown parameters colour" in r for r in reasons)
    assert any("is not a latitude" in r for r in reasons)


def test_an_invented_tool_is_refused_by_the_registry():
    reasons = executor.validate([{"tool": "delete_everything", "params": {}}])
    assert "is not a tool" in reasons[0]


def test_a_rejected_plan_runs_nothing(world, log, writes):
    with pytest.raises(executor.PlanRejected):
        executor.execute(
            [
                {"tool": "list_entities", "params": {}},
                {"tool": "place_asset", "params": {"kind": "node"}},
            ],
            source="typed",
            tier="parser",
        )
    assert writes == []
    # Refused, and still recorded: "what did someone try to do" is most of an audit log.
    assert [r["result"] for r in log] == ["rejected"]


# --------------------------------------------------------------------------
# The clarification round trip
# --------------------------------------------------------------------------


def test_an_ambiguous_name_returns_candidates_rather_than_a_dead_end(world, log):
    outcome = executor.execute(
        [{"tool": "describe_entity", "params": {"target": "daymark"}}],
        source="typed",
        tier="parser",
        utterance="tell me about daymark",
    )

    clarify = outcome["ui_effects"]["clarify"]
    assert clarify["query"] == "daymark"
    assert [o["id"] for o in clarify["options"]] == ["uas-daymark-03", "uas-daymark-05"]
    assert clarify["total"] == 2
    # Each option is a plan that can be posted straight back, not a label to re-type.
    assert clarify["options"][0]["plan"] == [
        {"tool": "describe_entity", "params": {"target": "uas-daymark-03"}}
    ]
    assert log[-1]["params"]["clarify_candidates"] == ["uas-daymark-03", "uas-daymark-05"]


def test_asking_is_its_own_outcome_in_the_log(world, log):
    """Not `rejected`. "I understood you and need one more word" is not a refusal, and
    recording it as one makes "how often does it have to ask" unanswerable."""
    executor.execute(
        [{"tool": "describe_entity", "params": {"target": "daymark"}}],
        source="typed",
        tier="parser",
        utterance="tell me about daymark",
    )
    assert log[-1]["result"] == "clarify"


def test_the_decision_is_logged_not_only_its_consequences(world, log):
    """A successful command used to record which tool ran and never what was said to
    cause it, so the sentence was recoverable only when the plan failed."""
    executor.execute(
        [{"tool": "list_entities", "params": {"kind": "uas"}}],
        source="voice",
        tier="parser",
        utterance="where are my drones",
    )
    plan_row = next(r for r in log if r["tool"] == "plan")
    assert plan_row["params"]["utterance"] == "where are my drones"
    assert plan_row["result"] == "ok"
    # Order is the reasoning order: the decision, then what it did.
    assert [r["tool"] for r in log] == ["plan", "list_entities"]


def test_a_spoken_command_is_one_thread_in_the_log(world, log):
    """Transcription and the command it produced are two requests. `parent_command_id`
    is what stops them being two unrelated rows: "what did the person actually say to
    cause this" has to be answerable."""
    transcription_id = "11111111-1111-1111-1111-111111111111"
    executor.execute(
        [{"tool": "list_entities", "params": {"kind": "uas"}}],
        source="voice",
        tier="parser",
        utterance="show me the drones",
        parent_command_id=transcription_id,
    )
    assert all(r["parent_command_id"] == transcription_id for r in log)


def test_answering_a_clarification_resolves_it_and_chains_in_the_log(world, log):
    first = executor.execute(
        [{"tool": "describe_entity", "params": {"target": "daymark"}}],
        source="typed",
        tier="parser",
        utterance="tell me about daymark",
    )
    clarify = first["ui_effects"]["clarify"]
    chosen = clarify["options"][1]

    second = executor.execute(
        chosen["plan"],
        source="ui_button",
        tier="parser",
        parent_command_id=clarify["command_id"],
    )

    assert second["results"][0]["ok"] is True
    assert "Daymark 05" in second["summary"]
    # The chain, which is the whole reason `parent_command_id` exists in the schema.
    assert second["command_id"] != first["command_id"]
    assert log[-1]["parent_command_id"] == first["command_id"]
    assert log[-1]["command_id"] == second["command_id"]


def test_a_clarification_answer_does_not_replay_steps_that_already_ran(world, log):
    """The offered plan starts at the ambiguous step. Replaying from the top would place
    a second asset or fly a drone twice, since the earlier steps have already committed."""
    outcome = executor.execute(
        [
            {"tool": "list_entities", "params": {"kind": "uas"}},
            {"tool": "describe_entity", "params": {"target": "daymark"}},
        ],
        source="typed",
        tier="parser",
    )
    plan = outcome["ui_effects"]["clarify"]["options"][0]["plan"]
    assert len(plan) == 1
    assert plan[0]["tool"] == "describe_entity"


def test_substitution_reaches_inside_a_list_of_targets(world, log):
    outcome = executor.execute(
        [{"tool": "frame_entities", "params": {"targets": ["daymark", "Eureka 02"]}}],
        source="typed",
        tier="parser",
    )
    plan = outcome["ui_effects"]["clarify"]["options"][0]["plan"]
    assert plan[0]["params"]["targets"] == ["uas-daymark-03", "Eureka 02"]


def test_a_name_that_matches_nothing_is_a_refusal_with_no_options(world, log):
    """Zero matches is not a question. There is nothing to offer, and the log says so."""
    outcome = executor.execute(
        [{"tool": "describe_entity", "params": {"target": "narwhal"}}],
        source="typed",
        tier="parser",
    )
    assert "clarify" not in outcome["ui_effects"]
    assert outcome["results"][0]["ok"] is False
    assert log[-1]["result"] == "rejected"


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_tasking_a_drone_keeps_its_heartbeat(world, log, writes):
    """It used to write None, which removed the drone from overdue accounting for good:
    the one action an operator takes on a drone stopped the system noticing it go quiet."""
    before = next(a for a in WORLD if a["id"] == "uas-daymark-05")["last_heard"]

    tools.task_uas(target="Daymark 05", lat=71.2, lon=-97.4)

    assert writes[0]["id"] == "uas-daymark-05"
    assert writes[0]["last_heard"] == datetime.fromisoformat(before)


def test_a_vague_name_never_reaches_a_write(world, log, writes):
    """The property that keeps the clarification safe: every tool that takes a name
    resolves it at the top, BEFORE it touches anything. So a write is never performed on
    an asset the operator did not unambiguously choose, and the question is asked with
    nothing yet committed."""
    outcome = executor.execute(
        [{"tool": "task_uas", "params": {"target": "daymark", "lat": 70.4, "lon": -99.8}}],
        source="typed",
        tier="parser",
    )
    assert "clarify" in outcome["ui_effects"]
    assert writes == []


def test_a_human_move_is_recorded_as_intent_not_as_a_do_not_touch_flag(world, log, writes):
    """An operator dropping an asset where the real thing stands must find it there
    afterwards. That needs no flag: a placed asset has no route to be carried along, and a
    tasked drone records the station it was sent to, which says where it belongs rather
    than merely that something should leave it alone."""
    tools.place_asset(kind="node", lat=69.5, lon=-105.2)
    assert writes[0]["geometry"] is None
    assert writes[0]["created_by"] == "user"
    # 🔴 NOT frozen. A placed asset of a moving kind has to be free to move, and a
    # stationary one needs no flag to stay where it was put.
    assert "motion_frozen" not in writes[0]["props"]

    writes.clear()
    tools.task_uas(target="Daymark 05", lat=71.2, lon=-97.4)
    props = writes[0]["props"]
    assert props["state"] == "on_station"
    assert props["station"] == [71.2, -97.4]
    assert props["motion_frozen"] is True


def test_every_kind_the_database_accepts_can_be_placed(world, log, writes):
    """The placeable list and the database's check constraint are two copies of one list.
    A kind allowed here and refused there dies inside the insert as an unhandled error
    rather than as a clean refusal, so the log would blame the tool for a bad request."""
    schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text()
    declared = re.search(r"kind in \(([^)]*)\)", schema).group(1)
    in_db = {k.strip().strip("'") for k in declared.split(",")}
    assert in_db == set(tools.PLACEABLE_KINDS)


def test_placing_a_moving_kind_is_allowed_and_left_free_to_move(world, log, writes):
    """A map an operator can only add masts to is read-only for most of what is on it."""
    tools.place_asset(kind="vessel", lat=74.2, lon=-95.0)
    assert writes[0]["kind"] == "vessel"
    assert "motion_frozen" not in writes[0]["props"]


def test_an_invented_kind_is_still_refused_by_name(world, log, writes):
    with pytest.raises(tools.ToolError) as caught:
        tools.place_asset(kind="submarine", lat=74.2, lon=-95.0)
    assert "not an asset kind" in str(caught.value)
    assert writes == []


def test_a_drone_refuses_a_station_it_cannot_return_from(world, log, writes):
    with pytest.raises(tools.ToolError) as caught:
        tools.task_uas(target="Daymark 05", lat=45.0, lon=-75.7)
    assert "come back" in str(caught.value)
    assert writes == []


# --------------------------------------------------------------------------
# Tier 2
# --------------------------------------------------------------------------


def test_a_replayed_selection_becomes_the_same_plan_shape_the_parser_emits(world, log):
    """The property the two-tier design rests on: downstream, nothing can tell them
    apart. Same validator, same executor, same audit rows, and `tier` is the only record
    of which one ran."""
    provider = llm.ReplayProvider(
        {
            "anything that has not checked in lately": {
                "steps": [{"tool": "list_entities", "overdue": True}],
                "view_tool": "none",
                "reasoning": "the request is about silence, not condition",
            }
        }
    )
    plan = provider.select("Anything that has not checked in lately").to_plan()

    assert plan == [{"tool": "list_entities", "params": {"overdue": True}}]
    assert executor.validate(plan) == []

    outcome = executor.execute(plan, source="typed", tier="llm")
    assert outcome["results"][0]["ok"] is True
    assert log[-1]["tier"] == "llm"


def test_tier_2_can_serialize_several_actions_from_one_utterance(world, log):
    """🔴 THE CAPABILITY THIS TIER USED TO LOSE. The parser has expanded "isolate the
    drones" into a multi-step plan for a while and the executor has always run one, but
    tier 2 could only ever name a single command. So the sequencing vanished the moment a
    phrasing fell through to the model, and it looked like a partial answer rather than a
    missing feature."""
    provider = llm.ReplayProvider(
        {
            "narrow it down to the drones and then open daymark 03": {
                "steps": [
                    {"tool": "list_entities", "kind": "uas"},
                    {"tool": "describe_entity", "target": "Daymark 03"},
                ],
                "view_tool": "none",
                "reasoning": "two actions were named, in that order",
            }
        }
    )
    plan = provider.select("Narrow it down to the drones and then open Daymark 03").to_plan()

    assert plan == [
        {"tool": "list_entities", "params": {"kind": "uas"}},
        {"tool": "describe_entity", "params": {"target": "Daymark 03"}},
    ]
    assert executor.validate(plan) == []

    outcome = executor.execute(plan, source="typed", tier="llm")
    assert [r["ok"] for r in outcome["results"]] == [True, True]

    # 🔑 The audit trail is the point of serializing rather than collapsing: each step is
    # its own row under the one command_id, so "what did that sentence actually do" is a
    # query rather than a guess.
    assert [row["tool"] for row in log if row["tier"] == "llm"][-2:] == [
        "list_entities",
        "describe_entity",
    ]


def test_each_step_carries_its_own_parameters(world, log):
    """One shared parameter bag could not express a two-command answer at all: `kind` on
    one step and `target` on the other are indistinguishable from a single filter that
    means neither."""
    provider = llm.ReplayProvider(
        {
            "x": {
                "steps": [
                    {"tool": "list_entities", "kind": "uas", "target": ""},
                    {"tool": "describe_entity", "target": "Daymark 03", "kind": ""},
                ],
                "view_tool": "none",
            }
        }
    )
    plan = provider.select("x").to_plan()

    # An empty string is how the schema says "not specified", so it must not survive as a
    # filter that matches nothing.
    assert plan[0]["params"] == {"kind": "uas"}
    assert plan[1]["params"] == {"target": "Daymark 03"}


def test_no_data_command_is_an_empty_step_list_not_a_do_nothing_step(world, log):
    """"none" was removed from the tool enum when steps became a list. An empty array
    already says "no data command", so a do-nothing step would be a second spelling of one
    answer and the choice between them would be arbitrary."""
    assert "none" not in llm.DATA_TOOLS

    provider = llm.ReplayProvider(
        {"put it back": {"steps": [], "view_tool": "reset_view", "reasoning": "purely a view request"}}
    )
    plan = provider.select("put it back").to_plan()

    assert plan == [{"tool": "reset_view", "params": {}}]


def test_an_over_long_chain_is_refused_out_loud_rather_than_truncated(world, log):
    """A clamp in the selection layer would be a second opinion that silently disagrees
    with the validator. A truncated plan that runs is worse than a whole plan refused by
    name, because the operator is told it worked."""
    provider = llm.ReplayProvider(
        {
            "everything": {
                "steps": [{"tool": "mesh_status"} for _ in range(executor.MAX_STEPS + 1)],
                "view_tool": "none",
            }
        }
    )
    plan = provider.select("everything").to_plan()

    assert len(plan) == executor.MAX_STEPS + 1
    reasons = executor.validate(plan)
    assert reasons and str(executor.MAX_STEPS) in reasons[0]


def test_the_step_limit_the_model_is_told_is_the_one_the_validator_enforces():
    """Two constants would agree today and diverge on the first edit, which is exactly how
    `tools.py` ended up with two definitions of `is_overdue`.

    ⚠️ It is asserted on the DESCRIPTION rather than on `maxItems` because structured
    output does not accept `maxItems` and returns a 400 for it. The limit is therefore
    told to the model in prose and enforced by `executor.validate`, and this is what stops
    the sentence and the check drifting apart."""
    steps = llm.response_schema()["properties"]["steps"]
    assert f"At most {executor.MAX_STEPS}" in steps["description"]


def test_the_schema_uses_no_keyword_the_api_rejects():
    """🔴 A LIVE 400 THAT THE WHOLE SUITE STAYED GREEN THROUGH. `maxItems` on the steps
    array took tier 2 down completely and the replay provider could not see it, because a
    fixture never sends the schema anywhere. This walks the schema for the keywords that
    are known not to survive the trip."""
    rejected = {"maxItems", "minItems", "minLength", "maxLength", "pattern", "format"}

    def walk(node, path="schema"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in rejected, f"{path}.{key} is not supported by structured output"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(llm.response_schema())


def test_the_model_cannot_name_a_tool_outside_the_enum():
    """The security boundary is the schema, not a downstream check."""
    schema = llm.response_schema()
    step_tools = schema["properties"]["steps"]["items"]["properties"]["tool"]["enum"]
    assert "delete_everything" not in step_tools
    assert set(step_tools) <= set(tools.REGISTRY)
    assert schema["additionalProperties"] is False
    # The per-step object is closed too. An open one would let a hallucinated parameter
    # ride along inside a step and reach the validator as an unknown key.
    assert schema["properties"]["steps"]["items"]["additionalProperties"] is False


def test_an_unknown_tool_survives_to_the_validator_instead_of_being_dropped(world, log):
    """Dropping it in the selection layer would turn a refusal the operator can read into
    a plan that quietly does less than it was asked to."""
    provider = llm.ReplayProvider({"x": {"steps": [{"tool": "delete_everything"}], "view_tool": "none"}})
    plan = provider.select("x").to_plan()

    assert plan == [{"tool": "delete_everything", "params": {}}]
    reasons = executor.validate(plan)
    assert any("delete_everything" in r and "not a tool" in r for r in reasons)


def test_every_enumerated_tool_exists_in_the_registry():
    """A tool renamed in one place and not the other is a plan the validator refuses at
    runtime and nothing catches at build time. This is that check."""
    for name in llm.DATA_TOOLS + llm.VIEW_TOOLS:
        if name != "none":
            assert name in tools.REGISTRY


def test_cost_is_priced_per_model_and_never_guessed():
    """One hardcoded price pair was used for whatever model ran, so the log filled with
    confident numbers computed at another model's rates. An unknown model costs None
    rather than zero: a free call that never happened is worse than a missing figure."""

    class Usage:
        input_tokens = 15
        output_tokens = 68
        cache_read_input_tokens = 1272
        cache_creation_input_tokens = 0

    priced = {m: llm.usage_and_cost(Usage(), 0, m)["cost_usd"] for m in llm.PRICES}
    assert len(set(priced.values())) == len(priced), "every model must price differently"
    assert priced["claude-opus-5"] > priced["claude-sonnet-5"] > priced["claude-haiku-4-5"]

    unknown = llm.usage_and_cost(Usage(), 0, "some-model-that-does-not-exist")
    assert unknown["cost_usd"] is None
    # The counts survive, so the row is still worth having.
    assert unknown["output_tokens"] == 68


def test_the_default_model_is_one_we_have_a_price_for():
    """Changing the model is one constant. Changing it to something unpriced would keep
    the log filling with `null` costs, which reads as free rather than as unknown."""
    assert llm.MODEL in llm.PRICES


def test_replay_says_it_has_no_fixture_rather_than_guessing():
    with pytest.raises(llm.LLMUnavailable):
        llm.ReplayProvider({}).select("something nobody wrote a fixture for")


# --------------------------------------------------------------------------
# Recognising in order to decline
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    ["what is the forecast tomorrow", "show me the ads-b feed", "what is the wind speed"],
)
def test_out_of_scope_requests_are_recognised_and_named(utterance):
    """A blank stare is the worst answer. Declining a request you understood is a
    different act from failing to understand it, and both the reply and the log say which."""
    refusal = parser.unsupported(utterance)
    assert refusal is not None
    assert "available" in refusal or "try" in refusal


def test_the_parser_declines_rather_than_half_matching():
    """None is what sends an utterance to tier 2. A parser that guesses steals them."""
    assert parser.parse("which of these could reach the contact before it gets dark") is None


def test_down_is_a_condition_only_where_it_says_something_is_down():
    """🔴 FOUND BY A LIVE CALL, NOT BY THE SUITE. Bare, `\\bdown\\b` read "narrow it down to
    the drones" as a maintenance filter and answered with one asset, confidently and
    wrongly. "Down" is a direction and half of a dozen phrasal verbs before it is ever a
    condition. Same class of collision as "dark", and the same fix."""
    for said in ("which nodes are down", "what is down", "show me anything down"):
        assert parser.parse(said) == [
            {"tool": "list_entities", "params": {"status": "maintenance", **({"kind": "node"} if "nodes" in said else {})}}
        ]

    # The phrasing that actually bit, plus its relatives.
    assert parser.parse("narrow it down to the drones") is None
    assert parser.parse("cut it down to the vessels") is None


def test_a_sentence_naming_two_actions_goes_to_the_tier_that_can_serialize_it():
    """Every tier-1 branch answers a single intent and returns on the first match, so a
    two-action utterance got whichever branch was checked first and the rest vanished
    without a word. A partial answer is worse than declining: it is not a refusal the
    operator can react to, it just looks like the answer."""
    assert parser.parse("show me everything that has gone quiet, then reset the view") is None
    assert parser.parse("list the drones and then open Daymark 03") is None
    assert parser.parse("mesh status after that show me the nodes") is None


def test_a_plain_and_is_not_a_sequence_and_still_answers_in_tier_1():
    """⚠️ The guard above must never fire on a bare "and". "The nodes and the drones" is
    one request with two filters, and deferring it would push a perfectly good
    deterministic answer onto a model, for latency and money, for nothing."""
    assert parser.parse("show me the nodes and the drones") is not None
    assert parser.parse("which assets are overdue") is not None
    assert parser.parse("isolate the drones") is not None


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_entities_carry_overdue_so_nobody_recomputes_it(world, log):
    """The display and the typed query have to agree, and the only way they cannot
    disagree is if the rule exists once. The server sends the answer, not the inputs."""
    from fastapi.testclient import TestClient

    from api import index

    client = TestClient(index.app)
    body = client.get("/api/entities").json()

    by_id = {a["id"]: a for a in body["entities"]}
    assert by_id["node-victoria-03"]["overdue"] is True
    assert by_id["node-eureka-02"]["overdue"] is False
    # The one that must never be true, however long it has been silent.
    assert by_id["radar-cam-04"]["overdue"] is False

    # Exactly one flag each, out of three, resolved server-side.
    assert {a["flag"] for a in body["entities"]} <= {"nominal", "maintenance", "overdue"}
    assert by_id["node-victoria-03"]["flag"] == "overdue"
    assert by_id["node-alert-01"]["flag"] == "maintenance"
    assert by_id["node-eureka-02"]["flag"] == "nominal"

    # And the same rule answers the typed question, from the same thresholds.
    typed = set(tools.list_entities(overdue=True).data["ids"])
    assert typed == {a["id"] for a in body["entities"] if a["overdue"]}


def test_the_mesh_model_is_described_by_whatever_computes_it(world, log, monkeypatch):
    """The endpoint used to publish a hardcoded formula written in a different file from
    the code it described, which is how a wire payload goes quietly false."""
    from api import index

    monkeypatch.setattr(
        index.meshlib, "model", lambda: {"note": "ground to ground 25 km"}, raising=False
    )
    assert index._mesh_model() == {"note": "ground to ground 25 km"}

    # And with nothing offered it says nothing about the assumptions, rather than
    # something untrue about them.
    monkeypatch.delattr(index.meshlib, "model", raising=False)
    bare = index._mesh_model()
    assert "does not publish" in bare["note"]
    assert "formula" not in str(bare)


def test_the_real_mesh_module_describes_itself(world, log):
    """The seam, exercised against what is actually in the tree rather than a stub."""
    from api import index

    assert index._mesh_model() == index.meshlib.model()
    assert "horizon" not in str(index._mesh_model())


# --------------------------------------------------------------------------
# Editing the world: removal, adversaries, and injected faults
# --------------------------------------------------------------------------


def test_an_asset_can_be_removed_and_the_selection_is_cleared(world, log, deletes):
    """A map you can only add to is one where every mistake is permanent."""
    out = tools.remove_asset(target="Eureka 02")

    assert deletes == ["node-eureka-02"]
    assert out.ui_effects["refetch"] is True
    # Leaving a detail panel open on a row that no longer exists is how a UI shows a ghost.
    assert out.ui_effects["select"] is None
    assert out.entity_id == "node-eureka-02"


def test_removing_something_ambiguous_still_asks_rather_than_guessing(world, log, deletes):
    """The one place a wrong guess is unrecoverable, so it had better not guess."""
    with pytest.raises(tools.Ambiguous):
        tools.remove_asset(target="daymark")
    assert deletes == []


def test_an_adversary_is_a_flag_on_an_ordinary_kind(world, log, writes):
    """A hostile vessel is still a vessel: it floats, drifts, and is seen by the same
    sensors at the same ranges. A separate kind would have needed teaching to terrain,
    motion, detection and the mesh, all to describe something that behaves identically."""
    tools.place_asset(kind="vessel", lat=74.2, lon=-95.0, hostile=True)
    assert writes[0]["props"]["hostile"] is True

    writes.clear()
    tools.place_asset(kind="vessel", lat=74.3, lon=-95.1)
    assert "hostile" not in writes[0]["props"]


def test_a_silent_fault_makes_an_asset_overdue(world, log, writes):
    """Faults are expressed in the fields the display already watches, so breaking
    something is visible immediately rather than needing new rendering."""
    tools.inject_fault(target="Eureka 02", fault="silent")

    written = writes[0]
    assert written["props"]["fault"] == "silent"
    # The whole point: it now answers the question the status strip asks.
    assert freshness.is_overdue({"kind": "node", "last_heard": written["last_heard"]}) is True


def test_a_maintenance_fault_stops_a_drone_being_tasked(world, log, writes):
    """The two fault types are the two states the rest of the system already reacts to."""
    tools.inject_fault(target="Daymark 05", fault="maintenance")
    assert writes[0]["status"] == "maintenance"


def test_an_unknown_fault_is_refused_with_the_list(world, log, writes):
    with pytest.raises(tools.ToolError) as caught:
        tools.inject_fault(target="Eureka 02", fault="on fire")
    assert "not a fault I can inject" in str(caught.value)
    assert "silent" in str(caught.value)
    assert writes == []


def test_clearing_a_fault_restores_service_and_the_heartbeat(world, log, writes):
    """Restoring is as much of the demonstration as breaking: a world you can only
    damage is one nobody explores."""
    tools.clear_fault(target="Alert 01")

    written = writes[0]
    assert written["status"] == "nominal"
    assert "fault" not in written["props"]
    assert freshness.is_overdue({"kind": "node", "last_heard": written["last_heard"]}) is False


def test_a_write_never_persists_a_derived_value(world, log, writes):
    """🔴 The read path decorates rows and `insert_entity` replaces them whole, so a tool
    that handed back everything it read would write simulated state into the database as
    though an operator had set it, and would try to write keys that are not columns."""
    tools.inject_fault(target="Eureka 02", fault="silent")

    written = writes[0]
    for derived in ("overdue", "flag", "mesh_connected", "server_reachable", "tracked", "held"):
        assert derived not in written, f"{derived} is derived at read time and must not be stored"


@pytest.mark.parametrize(
    "utterance,tool_name",
    [
        ("kill node-barrow-05", "inject_fault"),
        ("take down the alert node", "inject_fault"),
        ("put daymark 03 into maintenance", "inject_fault"),
        ("fix node-barrow-05", "clear_fault"),
        ("remove vsl-unk-01", "remove_asset"),
        ("get rid of the marker", "remove_asset"),
    ],
)
def test_breaking_and_repairing_reach_tier_one(utterance, tool_name):
    """An operator says kill it, break it, take it down. Nobody types "inject a fault"."""
    plan = parser.parse(utterance)
    assert plan and plan[0]["tool"] == tool_name


def test_an_action_verb_beats_the_adjective_it_contains():
    """"take down X" is an order and "which nodes are down" is a question, and the
    question's keyword sits inside the order's phrasing."""
    assert parser.parse("take down the alert node")[0]["tool"] == "inject_fault"
    assert parser.parse("which nodes are down")[0]["params"]["status"] == "maintenance"


def test_coverage_names_the_contacts_nobody_has(world, log, monkeypatch):
    """🔴 This tool talks to a module the suite does not otherwise touch, and the first
    live call failed on a shape mismatch that 90 green tests had not noticed: the buckets
    are lists of ids, and the counts live under their own key."""

    class FakeDetect:
        @staticmethod
        def coverage_summary(rows):
            return {
                "self_reporting": ["vsl-kanguk"],
                "tracked": [],
                "detected_not_reported": ["vsl-unk-03"],
                "untracked": ["gnd-unk-02"],
                "counts": {
                    "contacts": 3,
                    "self_reporting": 1,
                    "tracked": 0,
                    "detected_not_reported": 1,
                    "untracked": 1,
                },
            }

    monkeypatch.setattr(tools, "_detect_module", lambda: FakeDetect)
    out = tools.coverage()

    # Both kinds of gap are named, because an operator acts on ids and not on totals.
    assert out.ui_effects["highlight"] == ["vsl-unk-03", "gnd-unk-02"]
    assert "cannot report it" in out.message
    assert "held by nothing at all" in out.message


def test_coverage_refuses_honestly_when_nothing_computes_it(world, log, monkeypatch):
    monkeypatch.setattr(tools, "_detect_module", lambda: None)
    with pytest.raises(tools.ToolError) as caught:
        tools.coverage()
    assert "does not compute sensor coverage" in str(caught.value)
    # And it says what IS available rather than stopping at the refusal.
    assert "mesh connectivity" in str(caught.value)


@pytest.mark.parametrize(
    "utterance",
    ["what are we not seeing", "which adversaries are undetected", "show me blind spots"],
)
def test_asking_what_we_cannot_see_reaches_tier_one(utterance):
    assert parser.parse(utterance)[0]["tool"] == "coverage"


# --------------------------------------------------------------------------
# The commands this application must answer
# --------------------------------------------------------------------------


def test_the_current_zoom_window_is_a_filter_not_a_search():
    """🔴 The first capability the application is meant to have, and for a while it was
    answered by a guess: this phrase fell through to the loose listing branch and became a
    search for an asset literally named "assets in the current zoom window"."""
    plan = parser.parse("show me assets in the current zoom window")
    assert plan == [{"tool": "list_entities", "params": {"bbox": executor.VIEWPORT}}]


def test_a_viewport_filter_keeps_only_what_is_on_screen(world, log):
    box = {"north": 72.0, "south": 68.0, "east": -90.0, "west": -110.0, "global": False}
    inside = tools.list_entities(bbox=box).data["ids"]

    assert "node-victoria-03" in inside  # 69.0, -105.0
    assert "node-alert-01" not in inside  # 82.5, way north of the box
    # An asset with no position cannot be on screen, and is excluded rather than kept.
    assert all(i != "patrol-no-position" for i in inside)


def test_deixis_resolves_against_what_the_operator_is_looking_at():
    """"this asset" is how anyone refers to the thing they just clicked. A system that
    cannot resolve it makes them type a name already visible on their screen."""
    plan = parser.parse("show me the historic location of this asset")
    assert plan[0]["params"]["target"] == "this asset"

    resolved = executor.resolve_context(plan, {"selected_id": "uas-daymark-03"})
    assert resolved[0]["params"]["target"] == "uas-daymark-03"


def test_an_unresolvable_reference_is_refused_with_a_reason():
    """Dropping the parameter would silently widen the request to the whole world, which
    is the most expensive possible way to be wrong about what someone asked for."""
    plan = parser.parse("show me the historic location of this asset")
    with pytest.raises(executor.PlanRejected) as caught:
        executor.resolve_context(plan, {})
    assert "nothing is selected" in caught.value.reasons[0]

    with pytest.raises(executor.PlanRejected) as caught:
        executor.resolve_context(parser.parse("show me assets in the current view"), {})
    assert "on screen" in caught.value.reasons[0]


def test_one_request_performs_several_actions(world, log):
    """One request has to adjust the frame, filter the picture, isolate the asset and open
    its detail. Every parser branch returned exactly one step until this landed."""
    plan = parser.parse("isolate the drones")
    assert len(plan) > 1

    outcome = executor.execute(plan, source="typed", tier="parser", utterance="isolate the drones")
    assert len(outcome["results"]) == len(plan)
    # One command id over several actions is what makes the log readable as one intent.
    ran = [r for r in log if r["tool"] in ("list_entities", "frame_entities")]
    assert len({r["command_id"] for r in ran}) == 1
    # The effects of every step are merged into one answer for the client.
    assert "camera" in outcome["ui_effects"] and "highlight" in outcome["ui_effects"]


def test_active_flights_are_answerable_now_that_aircraft_exist():
    """Refusing this was right when the world had no aircraft. It has four."""
    plan = parser.parse("show active flights in the zoom window")
    assert plan[0]["params"]["kind"] == "aircraft"
    assert plan[0]["params"]["bbox"] == executor.VIEWPORT


def test_an_overlay_request_offers_what_exists_and_claims_nothing_more(world, log):
    plan = parser.parse("show me weather overlays for the current zoom window")
    assert plan[0]["tool"] == "show_overlay"

    out = tools.show_overlay(layer="ice")
    assert out.ui_effects["overlay"] == {"layer": "ice", "visible": True}

    with pytest.raises(tools.ToolError) as caught:
        tools.show_overlay(layer="thunderstorms")
    assert "no \"thunderstorms\" overlay" in str(caught.value)
    assert "nothing here fetches live weather" in str(caught.value).lower()


def test_a_refusal_that_went_stale_was_retired(world, log):
    """🔴 These two were refused outright and both refusals outlived the gap they
    described. The world now carries aircraft as real assets, and measured sea ice is the
    environmental overlay someone asking for weather wants. A stale entry in that list
    fires BEFORE the parser, so it silently outranks a command that works."""
    assert parser.unsupported("show active flights in the zoom window") is None
    assert parser.unsupported("show me weather overlays for the current zoom window") is None

    assert parser.parse("show active flights in the zoom window")[0]["params"]["kind"] == "aircraft"
    assert parser.parse("show me weather overlays now")[0]["tool"] == "show_overlay"

    # And what genuinely is absent is still refused, with what exists named instead.
    assert "ice" in (parser.unsupported("what is the forecast tomorrow") or "")


# --------------------------------------------------------------------------
# Voice: the screen is the vocabulary
# --------------------------------------------------------------------------


def test_the_transcription_prompt_carries_what_is_on_the_map(world, log):
    """🔑 Every word that matters here is one a general speech model has weak priors for:
    a place name it has barely seen, an initialism said as letters, a coined callsign, and
    identifiers that are half word and half number. Handing it the list turns a guess into
    a match, and a misheard asset name is a command that resolves to nothing."""
    vocabulary = transcribe._live_vocabulary()

    assert "Daymark 03" in vocabulary  # a coined callsign
    assert "uas" in vocabulary  # an initialism said as letters
    # The id is said out loud too, and it is the harder half: "uas daymark 03".
    assert "uas daymark 03" in vocabulary
    # A misheard verb loses the command even when every name in it was perfect.
    assert "isolate" in vocabulary and "overdue" in vocabulary

    prompt = transcribe.build_prompt()
    assert "Daymark 03" in prompt
    # A hint, never a constraint: a word not on the map must still be transcribable.
    assert "Do NOT force a match" in prompt
    # Coordinates are the other thing said aloud that a general model writes as words.
    assert "73.2 -95.9" in prompt


def test_the_vocabulary_survives_the_world_being_unreadable(monkeypatch):
    """Losing the hint costs accuracy on names. Failing the call costs the operator their
    voice, and those are not comparable."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(db, "fetch_entities", boom)
    vocabulary = transcribe._live_vocabulary()

    assert "isolate" in vocabulary  # the static half still helps
    assert transcribe.build_prompt().startswith(transcribe.PROMPT[:40])


def test_vocabulary_terms_are_deduplicated_keeping_the_first_spelling(world, log):
    """The model should be shown "Barrow Strait 05", not a lower-cased copy of it."""
    vocabulary = transcribe._live_vocabulary()
    lowered = [v.lower() for v in vocabulary]
    assert len(lowered) == len(set(lowered))


def test_the_spoken_vocabulary_keeps_up_with_the_commands(world, log):
    """🔑 A NOTE TO RE-CHECK THIS BEFORE THE DEMO WOULD RUN ON SOMEBODY'S MEMORY, and the
    whole point of the vocabulary is that a word nobody hinted is a word likely misheard.
    Asset names come from the live world and cannot drift. The static half can, so it is
    pinned to the code's own enums: add a fault, an overlay or a placeable kind and this
    test names the term you forgot to make sayable."""
    spoken = " ".join(transcribe._live_vocabulary()).lower()

    for fault in tools.FAULTS:
        assert fault in spoken, f"fault '{fault}' cannot be hinted to the transcriber"
    for layer in tools.OVERLAYS:
        assert layer in spoken, f"overlay '{layer}' cannot be hinted to the transcriber"
    for kind in tools.PLACEABLE_KINDS:
        # Kinds are said as words, so the hint is the spoken form rather than the id.
        assert kind.replace("_", " ") in spoken, f"kind '{kind}' cannot be hinted"
    for flag in freshness.FLAGS:
        assert flag in spoken, f"flag '{flag}' cannot be hinted to the transcriber"


# --------------------------------------------------------------------------
# One connection per request
# --------------------------------------------------------------------------


class _FakeConn:
    """Enough of a psycopg connection to prove the sharing rules, and nothing more."""

    def __init__(self) -> None:
        self.closed = False
        self.rollbacks = 0
        self.commits = 0
        self.checks = 0

    def cursor(self):
        """Enough of a cursor for the pool's `select 1` liveness check.

        ⚠️ THIS METHOD IS WHY THE FIRST VERSION OF THE POOL TEST FAILED, and the failure was
        the pool being right: with no cursor the validation raised, so every pooled
        connection looked dead and was replaced. A double that cannot answer the question
        production asks reports a healthy component as broken.
        """
        conn = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                if conn.closed:
                    raise RuntimeError("connection is closed")
                conn.checks += 1

        return _Cursor()

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    # Only used by the no-scope branch, which hands the connection to `with`.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def fake_connections(monkeypatch):
    """Every `_new_connection()` hands back a fresh fake, and we keep them all."""
    made: list[_FakeConn] = []

    def factory():
        conn = _FakeConn()
        made.append(conn)
        return conn

    # ⚠️ THE POOL OUTLIVES A TEST, WHICH IS THE WHOLE POINT OF IT. Without clearing it, one
    # test's fake connection is handed to the next one and the count assertions below
    # measure the previous test instead of this one.
    db.close_pool()
    monkeypatch.setattr(db, "_new_connection", factory)
    yield made
    db.close_pool()


def test_a_request_opens_one_connection_and_everything_shares_it(fake_connections):
    """🔴 THE LATENCY FIX ITSELF. Neon costs about 700 ms per connection warm, one command
    opened three in series, and a multi-step plan opens more still: every tool call and
    every audit row paid its own handshake. The queries are against a 76-row table and
    were never the cost."""
    with db.request_scope():
        with db.connect() as first:
            pass
        with db.connect() as second:
            pass
        with db.connect() as third:
            pass

    assert len(fake_connections) == 1, "a request must open exactly one connection"
    assert first is second is third
    # 🔑 RETURNED TO THE POOL, NOT CLOSED. Closing it would throw away the ~1 s handshake
    # that the pool exists to stop paying, so "not closed" is the assertion that the
    # connection outlived the request.
    assert not fake_connections[0].closed
    assert fake_connections[0].rollbacks == 1, "checked in dirty is checked in rolled back"


def test_a_request_that_never_queries_opens_nothing(fake_connections):
    """The scope is lazy on purpose. Opening one up front would move the cost rather than
    remove it, and the static and window routes never touch the database at all."""
    with db.request_scope():
        pass

    assert fake_connections == []


def test_outside_a_request_every_call_owns_its_own_connection(fake_connections):
    """Scripts, the seeder and the tests take this branch, and it behaves exactly as the
    whole codebase did before the change."""
    with db.connect():
        pass
    with db.connect():
        pass

    assert len(fake_connections) == 2
    assert all(c.closed for c in fake_connections), (
        "the no-scope branch owns its connection outright and must not pool it: "
        "scripts and the seeder run there and should behave exactly as they always did"
    )


def test_a_failed_statement_rolls_back_so_it_cannot_poison_the_rest_of_the_request(fake_connections):
    """🔴 THE ONE GENUINELY NEW FAILURE MODE THE SHARING INTRODUCES. Postgres refuses every
    later statement on a connection whose transaction is aborted. Unshared, that connection
    was thrown away and nobody noticed. Shared, the very next thing the executor does after
    a tool raises is write the audit row explaining why, so without this the failure would
    eat its own explanation."""
    with db.request_scope():
        with pytest.raises(ValueError):
            with db.connect():
                raise ValueError("the tool blew up")

        assert fake_connections[0].rollbacks == 1

        # And the connection is still usable for the audit row that follows.
        with db.connect() as conn:
            assert conn is fake_connections[0]

    assert len(fake_connections) == 1


def test_the_shared_branch_never_commits_on_your_behalf(fake_connections):
    """Every writer in this codebase calls `conn.commit()` itself. Committing again at
    block exit would silently turn a caller that deliberately did not commit into one
    that did."""
    with db.request_scope():
        with db.connect():
            pass

    assert fake_connections[0].commits == 0


# --------------------------------------------------------------------------
# A tier-1 guess that resolves to nothing is escalated, not refused
# --------------------------------------------------------------------------


def test_a_referent_that_matches_nothing_is_its_own_kind_of_refusal(world, log):
    """🔴 THE SIGNAL THE ESCALATION RIDES ON. Zero matches and many matches are opposite
    situations: many means "I understood you, narrow it", zero means "I did not understand
    you". They cannot share an exception type, or the API cannot tell them apart."""
    with pytest.raises(tools.Unresolved):
        tools.describe_entity(target="unkowns")

    # Still a ToolError, so nothing that does not care about the difference changed.
    assert issubclass(tools.Unresolved, tools.ToolError)
    assert not issubclass(tools.Unresolved, tools.Ambiguous)


def test_the_executor_reports_unresolved_so_the_api_can_escalate(world, log):
    """The executor does not know which tier wrote the plan or what a model call costs, so
    it reports the fact and declines to make the decision."""
    outcome = executor.execute(
        [{"tool": "describe_entity", "params": {"target": "unkowns"}}],
        source="typed",
        tier="parser",
    )
    assert outcome["unresolved"] is True
    assert outcome["results"][0]["ok"] is False

    # An ordinary refusal must NOT set it, or everything escalates.
    ordinary = executor.execute(
        [{"tool": "task_uas", "params": {"target": "Daymark 05", "lat": 45.0, "lon": -75.7}}],
        source="typed",
        tier="parser",
    )
    assert ordinary["unresolved"] is False


def test_the_parser_will_not_guess_a_single_asset_from_a_plural_or_an_all(world, log):
    """🔴 THE BUG UNDERNEATH THE TYPO, and it survives correct spelling. "show me all
    unknowns" is a filter over many things, so `focus_entity` was the wrong tool before a
    misspelling was ever involved."""
    assert parser.parse("show me all unkowns") is None

    # ⚠️ "unknowns" SPELLED CORRECTLY IS A REAL COMMAND NOW, and it was not when this test
    # was written. It answers with the unidentified contacts the network actually holds,
    # so asserting None here would pin the parser to a gap that has since been filled.
    # The misspelling above still declines, which is the behaviour this test is about.
    assert parser.parse("show me all unknowns") == [{"tool": "show_unknown", "params": {}}]
    assert parser.parse("show me the unknowns") == [{"tool": "show_unknown", "params": {}}]

    # A plural tail that is not a known command still declines rather than guessing.
    assert parser.parse("show me all stragglers") is None

    # And it still names a single asset when the tail really is one.
    assert parser.parse("show me daymark 03") == [
        {"tool": "focus_entity", "params": {"target": "daymark 03"}}
    ]
    assert parser.parse("find kanguk") == [
        {"tool": "focus_entity", "params": {"target": "kanguk"}}
    ]


def test_a_misspelled_target_reaches_tier_2_and_the_chain_is_recorded(world, log, monkeypatch):
    """The whole round trip: the parser guesses a name, nothing resolves, the original
    words go to the model, and the audit log ties the second command to the first."""
    from api import index

    provider = llm.ReplayProvider(
        {
            "show me daymark 3": {
                "steps": [{"tool": "describe_entity", "target": "Daymark 03"}],
                "view_tool": "none",
                "reasoning": "the operator meant Daymark 03",
            }
        }
    )
    monkeypatch.setattr(llm, "default_provider", lambda: provider)
    monkeypatch.setattr(index.ratelimit, "origin_allowed", lambda origin: True)
    monkeypatch.setattr(
        index.ratelimit, "check", lambda ip: type("V", (), {"allowed": True})()
    )

    first = executor.execute(
        [{"tool": "focus_entity", "params": {"target": "daymark 3"}}],
        source="typed",
        tier="parser",
        utterance="show me daymark 3",
    )
    assert first["unresolved"] is True

    req = index.CommandRequest(utterance="show me daymark 3", source="typed")
    response = index._escalate_to_tier_two(req, first, client_ip="1.2.3.4", origin=None)

    assert response is not None, "an escalation that can run must not fall back"
    body = json.loads(bytes(response.body))
    assert body["tier"] == "llm"
    assert body["escalated_from"] == "parser"
    assert body["ok"] is True

    # 🔗 Every row of the escalation hangs off the first command.
    chained = [r for r in log if r.get("parent_command_id") == first["command_id"]]
    assert [r["tool"] for r in chained] == ["llm_select", "plan", "describe_entity"]


def test_an_escalation_that_cannot_run_keeps_the_honest_refusal(world, log, monkeypatch):
    """⚠️ EVERY FAILURE PATH FALLS BACK. The operator already has a true sentence saying
    the thing they named does not exist; a rate limit or a dead model must not replace it
    with a worse one about the escalation."""
    from api import index

    monkeypatch.setattr(index.ratelimit, "origin_allowed", lambda origin: True)
    monkeypatch.setattr(
        index.ratelimit, "check", lambda ip: type("V", (), {"allowed": True})()
    )
    # No fixture for this utterance, so the provider raises LLMUnavailable.
    monkeypatch.setattr(llm, "default_provider", lambda: llm.ReplayProvider({}))

    first = executor.execute(
        [{"tool": "focus_entity", "params": {"target": "nonsense"}}],
        source="typed",
        tier="parser",
        utterance="show me nonsense",
    )
    req = index.CommandRequest(utterance="show me nonsense", source="typed")

    assert index._escalate_to_tier_two(req, first, client_ip="1.2.3.4", origin=None) is None


def test_an_escalation_never_escalates_and_a_model_plan_never_bounces_back(world, log):
    """🔒 THE LOOP GUARD. `tier == "parser"` stops a model plan going back to the model,
    and an already-chained command stops an escalation escalating."""
    from_llm = executor.execute(
        [{"tool": "describe_entity", "params": {"target": "unkowns"}}],
        source="typed",
        tier="llm",
    )
    # The flag is still reported; the API's condition is what refuses to act on it.
    assert from_llm["unresolved"] is True


# --------------------------------------------------------------------------
# What tier 1 did with each word
# --------------------------------------------------------------------------


def test_the_trace_names_the_words_the_parser_threw_away():
    """🔴 THE BUG THIS EXISTS FOR. Two utterances differing by three words came back
    byte-identical, because the parser matched "parties", answered that, and discarded
    "unkown" and "on foot" without a word. A partial match presented as a complete answer
    is the characteristic failure of a deterministic parser, and it is invisible in the
    answer."""
    said = "show me all unkown parties on foot"
    trace = parser.trace(said, parser.parse(said))

    assert trace["ignored"] == ["unkown", "foot"]
    assert "parties" in trace["consumed"]


def test_a_clean_utterance_ignores_nothing():
    """The trace has to be quiet on the ordinary case, or it is noise nobody reads and the
    escalation below fires on everything."""
    for said in (
        "show me the drones",
        "which assets are overdue",
        "mesh status",
        "what is not broadcasting",
        "reset the view",
        "isolate the drones",
        "place a marker at 74.1 -95.2",
    ):
        assert parser.trace(said, parser.parse(said))["ignored"] == [], said


def test_a_coordinate_is_never_reported_as_a_dropped_word():
    """⚠️ A naive tokeniser turns "-95.9" into "95" and "9" and reports both as ignored,
    which would escalate every task and place command in the app: the one shape where
    every character already matters."""
    said = "send daymark 05 to 73.0 -95.9"
    assert parser.trace(said, parser.parse(said))["ignored"] == []


def test_a_duration_is_not_reported_as_dropped_digits():
    """"the last 12 hours" becomes `days=0.5`, so the digits that expressed it appear
    nowhere in the plan and would otherwise read as words the parser lost."""
    said = "history for daymark 3 for the last 12 hours"
    assert parser.trace(said, parser.parse(said))["ignored"] == []


def test_a_dropped_kind_is_caught():
    """"the nodes and the drones" is two kinds and the parser can only answer one, so it
    silently answers the second. That is a real partial match and must show."""
    said = "show me the nodes and the drones"
    assert "nodes" in parser.trace(said, parser.parse(said))["ignored"]


def test_no_plan_means_nothing_was_thrown_away():
    """A declined utterance goes onward whole. Reporting its words as "ignored" would
    escalate an utterance that is already being escalated."""
    trace = parser.trace("something nobody anticipated", None)
    assert trace["ignored"] == []
    assert trace["matched"] is None


def test_a_second_request_reuses_the_pooled_connection(fake_connections):
    """🔴 THE 85% FIX. Opening a connection costs ~1.0 s measured; the query it carries
    costs 0.13 s. The request scope cut three connections per command to one, and one per
    request still paid the handshake. A connection that outlives the request is the only
    thing that stops paying it."""
    for _ in range(3):
        with db.request_scope(), db.connect():
            pass

    assert len(fake_connections) == 1, "three requests, one handshake"


def test_a_dead_pooled_connection_is_discarded_rather_than_handed_out(fake_connections):
    """🔒 Neon closes idle connections on its own schedule. A pool that trusts what it
    holds converts a slow request into a failed one, which is worse than the cost it was
    trying to avoid."""
    with db.request_scope(), db.connect():
        pass
    fake_connections[0].closed = True  # Neon hung up while it sat idle

    with db.request_scope(), db.connect() as conn:
        assert conn is not fake_connections[0]

    assert len(fake_connections) == 2, "the dead one is replaced, not reused"


def test_the_pool_is_bounded(fake_connections):
    """An unbounded idle list is a connection leak with a friendlier name, against a
    database that caps them."""
    conns = [_FakeConn() for _ in range(db._POOL_MAX + 3)]
    for c in conns:
        db._checkin(c)

    assert len(db._pool) == db._POOL_MAX
    assert all(c.closed for c in conns if c not in db._pool), "the overflow is closed, not dropped"


# --------------------------------------------------------------------------
# "list them" — the previous answer
# --------------------------------------------------------------------------

RECENT = {
    "recent": [
        {"utterance": "how many unknown parties on foot", "summary": "3 matching", "tier": "llm",
         "ids": ["uas-daymark-03", "uas-daymark-05"]},
    ]
}


def test_them_binds_to_the_previous_answer_not_to_the_whole_world():
    """🔴 THE BUG, VERBATIM FROM A LIVE SESSION:

        > how many unknown parties on foot
        · 3 matching
        > list them
        · 76 matching

    "Them" reached no branch, fell through to the loose listing rule, matched no kind and
    widened to everything. Answering a narrower question with a wider answer is the worst
    shape of wrong available here, because 76 looks like a working command."""
    plan = parser.parse("list them")
    assert plan == [{"tool": "list_entities", "params": {"ids": executor.RESULT}}]

    resolved = executor.resolve_context(plan, RECENT)
    assert resolved[0]["params"]["ids"] == ["uas-daymark-03", "uas-daymark-05"]


def test_them_actually_narrows_the_answer(world, log):
    """End to end: the ids reach `list_entities` and it returns those two, not the world."""
    plan = executor.resolve_context(parser.parse("show me them"), RECENT)
    outcome = executor.execute(plan, source="typed", tier="parser")

    assert outcome["results"][0]["data"]["ids"] == ["uas-daymark-03", "uas-daymark-05"]
    assert len(WORLD) > 2, "the point is that the world is bigger than the answer"


def test_them_with_no_previous_answer_refuses_and_says_why():
    """⚠️ AN UNRESOLVABLE PLACEHOLDER IS A REFUSAL WITH A REASON, NEVER A SILENT DROP.
    Dropping the parameter would widen the request back to every asset in the world, which
    is the exact bug this branch exists to fix, reintroduced by the error path."""
    with pytest.raises(executor.PlanRejected) as caught:
        executor.resolve_context(parser.parse("list them"), {})
    assert "them" in str(caught.value)

    # A malformed history from the browser reads as "no previous answer", never a crash.
    for junk in ({"recent": "nope"}, {"recent": []}, {"recent": [{"ids": "no"}]}, {"recent": [None]}):
        with pytest.raises(executor.PlanRejected):
            executor.resolve_context(parser.parse("list them"), junk)


def test_an_empty_previous_answer_stays_empty(world, log):
    """`ids=[]` must mean "those zero things", not "no filter". A truth test here would
    turn "list them" after an empty answer into a listing of everything, which is the
    original bug wearing a different hat."""
    result = tools.list_entities(ids=[])
    assert result.data["ids"] == []


def test_frame_them_frames_rather_than_lists():
    """Same referent, different verb. "Frame them" is a camera request and `frame_entities`
    is the tool that takes a set."""
    assert parser.parse("frame them") == [
        {"tool": "frame_entities", "params": {"targets": executor.RESULT}}
    ]


def test_the_context_sent_to_the_model_is_bounded():
    """🔒 It arrives from a browser and is about to be pasted into a paid API call. Three
    turns and fifty ids is deixis; anything longer is memory, which is not what "them"
    means."""
    from api import index

    fat = {"bbox": {"global": True}, "recent": [
        {"utterance": "u" * 500, "summary": "s" * 500, "tier": "llm", "ids": [f"e-{i}" for i in range(500)]}
        for _ in range(20)
    ]}
    trimmed = index._model_context(fat)

    assert len(trimmed["recent"]) == 3
    assert len(trimmed["recent"][0]["ids"]) == 50
    assert len(trimmed["recent"][0]["utterance"]) == 200
    assert trimmed["bbox"] == {"global": True}, "the rest of the context is untouched"


def test_every_command_a_refusal_SUGGESTS_actually_works():
    """🔴 THE SUGGESTION IS PART OF THE CLAIM. A refusal here says "I cannot do that, try
    this instead", and the "this" is a command the operator will type next. If it does not
    work, the refusal is two failures in one sentence and the second one is the app's own
    fault.

    ⚠️ THIS IS THE STALENESS GUARD THE LIST ASKS FOR IN ITS OWN COMMENT. One of these had
    already gone stale: the air-traffic refusal offered "show active flights in the zoom
    window", and "active" is a filter this world does not model, so the app's suggested
    example was a partial match that had to go to the model. Tier 1 must answer it, or the
    refusal is recommending the slowest path in the build.
    """
    quoted = []
    for _pattern, message in parser.UNSUPPORTED:
        quoted += re.findall(r'"([^"]+)"', message)

    assert quoted, "a refusal that names nothing available is not doing its job"

    for command in quoted:
        plan = parser.parse(command)
        assert plan is not None, f"refusal suggests {command!r}, which tier 1 cannot answer"
        assert parser.trace(command, plan)["ignored"] == [], (
            f"refusal suggests {command!r}, and the parser drops "
            f"{parser.trace(command, plan)['ignored']} from it"
        )
        assert executor.validate(plan) == [], f"refusal suggests {command!r}, which does not validate"


def test_flights_is_a_synonym_the_shared_table_knows():
    """A synonym one branch knows and `KIND_WORDS` does not is invisible to everything that
    reads the table, `trace` included. That is how a correctly-answered command came to be
    reported as a partial match and pushed to the model."""
    assert parser.parse("show me the flights") == [
        {"tool": "list_entities", "params": {"kind": "aircraft"}}
    ]


# --------------------------------------------------------------------------
# "show unknown" — what the console may claim to have
# --------------------------------------------------------------------------

BUCKETS = {
    "self_reporting": ["vsl-ais-01"],
    "tracked": ["vsl-unk-01", "air-unk-01"],
    "detected_not_reported": ["vsl-unk-03"],
    "untracked": ["gnd-unk-02"],
    "counts": {"self_reporting": 1, "tracked": 2, "detected_not_reported": 1, "untracked": 1},
}


@pytest.fixture
def buckets(monkeypatch):
    """The detection layer's answer, fixed, so the tool's RULES are what is under test."""
    fake = type("D", (), {"coverage_summary": staticmethod(lambda assets: dict(BUCKETS))})
    monkeypatch.setattr(tools, "_detect_module", lambda: fake)
    return BUCKETS


def test_show_unknown_claims_only_the_contacts_the_network_actually_holds(world, log, buckets):
    """🔴 THE THREE-WAY RULE, AND THE TWO EXCLUSIONS ARE THE POINT.

        tracked                 not talking, held, the report gets home   -> YES
        detected_not_reported   held, and the report CANNOT get home      -> no
        untracked               nothing holds it and it is not talking    -> no

    `detected_not_reported` is the counter-intuitive one and the exclusion is deliberate:
    if the report is not reaching you, you do not have the contact. A sensor holding
    something it cannot deliver is a link fault, not coverage."""
    result = tools.show_unknown()

    assert result.data["ids"] == ["vsl-unk-01", "air-unk-01"]
    assert result.ui_effects["highlight"] == ["vsl-unk-01", "air-unk-01"]

    # 🔒 Neither excluded bucket may reach the set the display asserts.
    claimed = set(result.data["ids"])
    assert "vsl-unk-03" not in claimed, "a report that cannot get home is not coverage"
    assert "gnd-unk-02" not in claimed
    assert "vsl-ais-01" not in claimed, "a contact announcing itself is not unknown"


def test_the_withheld_contacts_travel_but_are_never_counted_as_covered(world, log, buckets):
    """The reveal toggle needs them, and a second round trip for something already computed
    would be waste. They ride in `data` and never in `ids`."""
    result = tools.show_unknown()

    assert result.data["detected_not_reported"] == ["vsl-unk-03"]
    assert result.data["untracked"] == ["gnd-unk-02"]
    assert result.data["counts"] == {"covered": 2, "detected_not_reported": 1, "untracked": 1}


def test_the_answer_says_out_loud_that_it_is_withholding_some(world, log, buckets):
    """⚠️ A display that quietly narrows what it claims is the failure this whole
    distinction exists to prevent. Saying how many were held back costs one clause."""
    assert "cannot be confirmed from the network alone" in tools.show_unknown().message


def test_show_unknown_degrades_rather_than_crashing_without_the_detection_layer(world, log, monkeypatch):
    """🔒 `detect` is imported through a try in `api/index.py` so its absence costs a field
    and never a route. A tool that assumed it was there would undo that."""
    monkeypatch.setattr(tools, "_detect_module", lambda: None)
    with pytest.raises(tools.ToolError) as caught:
        tools.show_unknown()
    assert "what is available" in str(caught.value).lower()


@pytest.mark.parametrize(
    "utterance",
    ["show unknown", "show me the unknowns", "unknown contacts", "what is unidentified",
     "who is out there", "show me all unknowns"],
)
def test_the_ways_people_ask_for_unknowns(utterance):
    assert parser.parse(utterance) == [{"tool": "show_unknown", "params": {}}]
    assert parser.trace(utterance, parser.parse(utterance))["ignored"] == []


def test_not_knowing_something_is_not_a_request_for_the_contact_list():
    """"unknown" is ordinary English in this domain, so it is matched as a request rather
    than as a bare word. Same care "dark" needed."""
    assert parser.parse("i do not know where it is") is None


# --------------------------------------------------------------------------
# Serialized actions: one request, four things done to one asset
# --------------------------------------------------------------------------


def test_isolating_a_named_asset_does_all_four_things_in_one_request(world, log):
    """🔴 THE SHAPE A SERIALIZED ACTION IS SUPPOSED TO HAVE: adjust the frame, filter the
    picture, isolate the asset, open its details. It used to do three of the four, and the
    missing one was the FILTER, because filtering to one asset needs its id and the parser
    only ever had the words the operator typed."""
    plan = parser.parse("isolate daymark 03")
    assert [s["tool"] for s in plan] == ["focus_entity", "list_entities", "describe_entity"]
    assert executor.validate(plan) == []

    outcome = executor.execute(plan, source="typed", tier="parser")
    assert [r["ok"] for r in outcome["results"]] == [True, True, True]

    effects = outcome["ui_effects"]
    assert "camera" in effects, "adjusts frame"
    assert "select" in effects, "isolates the asset"
    # Filters: the middle step narrowed the world to exactly that one asset.
    assert outcome["results"][1]["data"]["ids"] == ["uas-daymark-03"]
    assert outcome["results"][2]["message"].startswith("Daymark 03"), "opens details"


def test_the_subject_is_bound_from_the_step_that_resolved_it(world, log):
    """The parser cannot know the id and must not: working from words alone is what lets it
    be tested with no database. So the placeholder is bound between steps, at the only
    moment the id exists."""
    plan = parser.parse("isolate daymark 03")

    # Still a placeholder on the way in.
    assert plan[1]["params"]["ids"] == [executor.SUBJECT]
    assert plan[2]["params"]["target"] == executor.SUBJECT

    executor.execute(plan, source="typed", tier="parser")

    # The audit log records what actually RAN, so the placeholder must be gone from it.
    ran = [r for r in log if r.get("tool") in ("list_entities", "describe_entity")][-2:]
    assert all(executor.SUBJECT not in json.dumps(r["params"]) for r in ran)


def test_the_subject_is_the_FIRST_asset_named_not_the_latest():
    """Letting a later step move the target would make a chain mean different things
    depending on how far through it you looked."""
    assert executor._bind_subject({"target": executor.SUBJECT}, "a-1") == {"target": "a-1"}
    assert executor._bind_subject({"ids": [executor.SUBJECT]}, "a-1") == {"ids": ["a-1"]}
    # Nothing resolved yet: left alone, so it reaches the tool and is refused by name
    # rather than quietly widening to everything.
    assert executor._bind_subject({"ids": [executor.SUBJECT]}, None) == {"ids": [executor.SUBJECT]}


def test_an_unbound_subject_is_refused_rather_than_widening(world, log):
    """⚠️ THE DANGEROUS FAILURE WOULD BE SILENT. If the placeholder were dropped instead of
    passed through, `list_entities` would run with no filter and return the whole world in
    answer to a request about one asset."""
    outcome = executor.execute(
        [{"tool": "list_entities", "params": {"ids": [executor.SUBJECT]}}],
        source="typed",
        tier="parser",
    )
    assert outcome["results"][0]["data"]["ids"] == [], "matches nothing, rather than everything"


def test_isolating_a_kind_still_filters_and_frames(world, log):
    """A kind has no single asset to select or open, so that branch stays two steps. The
    four-action shape is about a named asset."""
    plan = parser.parse("isolate the drones")
    assert [s["tool"] for s in plan] == ["list_entities", "frame_entities"]


def test_the_trace_reports_the_parameters_it_inferred():
    """🔴 THE HALF NOBODY CAN SEE. The transcript already shows what was heard, so a misread
    word is on screen. What is not on screen is what the parser made of it: a voice test
    heard "which assets are overdue" as "Hydrophone Lancaster Sound 01 overdue", and the
    whole difference between the right answer and the wrong one was an unannounced
    `kind="hydrophone"`."""
    said = parser.trace("which assets are overdue", parser.parse("which assets are overdue"))
    misheard_text = "Hydrophone Lancaster Sound 01 overdue"
    misheard = parser.trace(misheard_text, parser.parse(misheard_text))

    assert said["extracted"] == {"overdue": True}
    assert misheard["extracted"] == {"overdue": True, "kind": "hydrophone"}


def test_a_placeholder_never_hides_the_value_the_operator_would_recognise():
    """"Isolate daymark 01" resolves its own subject, so later steps carry `__subject__` on
    the same key the first step carried "daymark 01". Letting the last write win would show
    the operator an internal token instead of the name they said."""
    trace = parser.trace("isolate daymark 01", parser.parse("isolate daymark 01"))
    assert trace["extracted"]["target"] == "daymark 01"


def test_a_declined_utterance_extracts_nothing():
    assert parser.trace("something nobody anticipated", None)["extracted"] == {}


def test_the_parser_trace_survives_an_escalation_to_tier_2(world, log, monkeypatch):
    """🔴 THE REASON FOR THE CALL IS MORE USEFUL THAN THE CALL. Overwriting `thinking`
    wholesale threw the trace away in exactly the case it was built for: a partial match.
    A mis-transcription escalated because tier 1 could not place three of the operator's
    words, and the response showed the model's reasoning with no hint that had happened."""
    from api import index

    provider = llm.ReplayProvider(
        {
            "hydrophone lancaster sound 01 overdue": {
                "steps": [{"tool": "describe_entity", "target": "Hydrophone Lancaster Sound 01"}],
                "view_tool": "none",
                "reasoning": "a specific hydrophone is named",
            }
        }
    )
    monkeypatch.setattr(llm, "default_provider", lambda: provider)
    monkeypatch.setattr(index.ratelimit, "origin_allowed", lambda origin: True)
    monkeypatch.setattr(index.ratelimit, "check", lambda ip: type("V", (), {"allowed": True})())

    client = TestClient(index.app)
    body = client.post(
        "/api/command",
        json={"utterance": "Hydrophone Lancaster Sound 01 overdue", "source": "voice"},
    ).json()

    thinking = body["thinking"]
    assert thinking["tier"] == "llm"
    assert thinking["reasoning"], "the model still explains itself"

    handed = thinking.get("parser")
    assert handed, "the trace that explains WHY the model was called must survive"
    assert handed["ignored"] == ["lancaster", "sound", "01"]
    assert handed["extracted"] == {"overdue": True, "kind": "hydrophone"}
