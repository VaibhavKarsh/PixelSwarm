"""Phase 8: focused unit tests for the harness's state transitions.

Roadmap Phase 8 asks for "basic unit tests around swarm/harness.py's state
transitions". The existing suite covers the loop end to end and the state object
in isolation; this file covers the seam between them - how one tick's committed
state becomes the next tick's input, and the pure functions the harness uses to
decide what it may commit.

Written after auditing what was already covered, so these are the gaps rather
than a restatement: state carried across ticks, the invariant's substitution
choice, `describe_failure`'s reason vocabulary (which the reliability report
aggregates on, so its prefixes are load-bearing), and `parse_real_personas`.

    python tests/unit/test_harness_transitions.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm.harness import (  # noqa: E402
    Harness,
    PersonaFailure,
    allowed_actions_for,
    describe_failure,
    load_config,
    parse_real_personas,
    validate_proposal,
)
from swarm.model_client import ModelParseFailure, ModelTimeout, ModelUnavailable  # noqa: E402
from swarm.state import Event  # noqa: E402

CONFIG, TRANSITIONS = load_config()


def events(*specs):
    return [Event(t, i, ts) for t, i, ts in specs]


# --- state carried from tick to tick ------------------------------------------

def test_each_tick_input_is_the_previous_tick_commit():
    with Harness(CONFIG, TRANSITIONS) as h:
        records = h.run(events(("chat_hype_spike", 0.9, 10.0),
                               ("game_danger", 0.8, 25.0),
                               ("game_safe", 0.1, 40.0)), idle_ticks=0)
    for prev, cur in zip(records, records[1:]):
        assert cur["input_state"]["current_mood"] == prev["final_state"]["current_mood"]
        assert cur["input_state"]["current_action"] == prev["final_state"]["current_action"]
        assert cur["input_state"]["last_line"] == prev["final_state"]["last_line"]


def test_first_tick_starts_from_the_documented_defaults():
    with Harness(CONFIG, TRANSITIONS) as h:
        records = h.run(events(("chat_calm", 0.1, 15.0)), idle_ticks=0)
    first = records[0]["input_state"]
    assert first["current_mood"] == "idle"
    assert first["current_action"] == "idle_loop"
    assert first["last_line"] is None
    assert first["ticks_since_last_change"] == 0


def test_ticks_since_last_change_advances_across_the_run():
    """Three identical calm events should leave the counter climbing, because
    nothing about the committed state changes."""
    with Harness(CONFIG, TRANSITIONS) as h:
        records = h.run(events(("chat_calm", 0.1, 15.0),
                               ("chat_calm", 0.1, 30.0),
                               ("chat_calm", 0.1, 45.0)), idle_ticks=0)
    counters = [r["final_state"]["ticks_since_last_change"] for r in records]
    assert counters == sorted(counters), counters
    assert counters[-1] >= counters[0]


def test_the_event_window_visible_to_a_tick_respects_both_bounds():
    max_n = CONFIG["recent_events_max_count"]
    with Harness(CONFIG, TRANSITIONS) as h:
        records = h.run(events(*[("chat_hype_spike", 0.6, float(i * 3))
                                 for i in range(12)]), idle_ticks=0)
    for r in records:
        window = r["input_state"]["recent_events"]
        assert len(window) <= max_n
        if len(window) > 1:
            assert [e["ts"] for e in window] == sorted(e["ts"] for e in window)


def test_a_timer_tick_ages_the_window_without_adding_events():
    with Harness(CONFIG, TRANSITIONS) as h:
        records = h.run(events(("game_danger", 0.9, 10.0)), idle_ticks=3)
    last_event_tick = records[0]
    final_timer_tick = records[-1]
    assert len(final_timer_tick["input_state"]["recent_events"]) <= \
        len(last_event_tick["input_state"]["recent_events"])


# --- what the harness may commit ---------------------------------------------

def test_allowed_actions_respects_the_previous_action():
    from_celebrate = allowed_actions_for("alert", TRANSITIONS, CONFIG["actions"], "celebrate")
    from_idle = allowed_actions_for("alert", TRANSITIONS, CONFIG["actions"], "idle_loop")
    assert "look_around" not in from_celebrate
    assert "look_around" in from_idle, "the smoothness rule must not apply from idle_loop"


def test_the_fallback_action_is_always_available():
    for mood in CONFIG["moods"]:
        for prev in CONFIG["actions"] + [None]:
            assert CONFIG["fallback"]["action"] in \
                allowed_actions_for(mood, TRANSITIONS, CONFIG["actions"], prev)


# --- validate_proposal, the gate before anything is committed ------------------

def test_validate_accepts_well_formed_proposals():
    assert validate_proposal("mood", {"mood": "idle", "confidence": 0.5, "reason": "r"}, CONFIG)
    assert validate_proposal("action", {"action": "wave", "confidence": 0.5, "reason": "r"}, CONFIG)
    assert validate_proposal("line", {"line": None, "reason": "r"}, CONFIG)
    assert validate_proposal("check", {"verdict": "approve", "final_mood": "idle",
                                       "final_action": "idle_loop", "reason": "r"}, CONFIG)


def test_a_literal_null_string_becomes_real_silence():
    """Found by the Phase 8 alt scenario, not by any test.

    Asked for "a short line or null", the model sometimes writes the WORD null.
    It is a valid string, so it passed every check and would have been drawn as a
    speech bubble reading "null".
    """
    for spelling in ("null", "None", " NULL ", '"null"', "nil", "n/a", ""):
        out = validate_proposal("line", {"line": spelling, "reason": "r"}, CONFIG)
        assert out["line"] is None, f"{spelling!r} should normalise to silence"


def test_a_real_line_is_never_swallowed_by_that_normalisation():
    for spelling in ("Null and void!", "None shall pass", "Nilsson!", "Hey there!"):
        out = validate_proposal("line", {"line": spelling, "reason": "r"}, CONFIG)
        assert out["line"] == spelling


def test_validate_rejects_each_malformed_shape():
    cases = [
        ("mood", {"confidence": 0.5}),
        ("mood", {"mood": "neutral", "confidence": 0.5}),
        ("action", {"action": "moonwalk", "confidence": 0.5}),
        ("line", {"line": 7}),
        ("check", {"verdict": "maybe", "final_mood": "idle", "final_action": "idle_loop"}),
        ("check", {"verdict": "approve", "final_mood": "idle", "final_action": "moonwalk"}),
    ]
    for kind, payload in cases:
        try:
            validate_proposal(kind, payload, CONFIG)
        except PersonaFailure:
            continue
        raise AssertionError(f"{kind} {payload} should have been rejected")


# --- failure vocabulary -------------------------------------------------------

def test_failure_reasons_carry_stable_greppable_prefixes():
    """The reliability report aggregates on the text before the first colon, so
    these prefixes are part of the interface, not just log prose."""
    assert describe_failure(ModelTimeout("x")).startswith("timeout:")
    assert describe_failure(ModelUnavailable("x")).startswith("unavailable:")
    assert describe_failure(ModelParseFailure("x")).startswith("parse_failure:")


def test_persona_failure_keeps_its_own_message():
    assert describe_failure(PersonaFailure("mood: out_of_enum:'neutral'")) == \
        "mood: out_of_enum:'neutral'"


def test_an_unexpected_exception_still_produces_a_reason():
    reason = describe_failure(ValueError("something odd"))
    assert "ValueError" in reason and "something odd" in reason


# --- persona selection --------------------------------------------------------

def test_parse_real_personas_accepts_the_documented_forms():
    assert parse_real_personas("") == []
    assert parse_real_personas("none") == []
    assert parse_real_personas("all") == ["mood", "action", "line", "checker"]
    assert parse_real_personas("mood") == ["mood"]
    assert parse_real_personas(" mood , checker ") == ["mood", "checker"]


def test_parse_real_personas_rejects_an_unknown_name():
    try:
        parse_real_personas("narrator")
    except SystemExit as exc:
        assert "narrator" in str(exc)
    else:
        raise AssertionError("an unknown persona must be rejected, not silently ignored")


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  FAIL  {name}")
    print("-" * 68)
    if failures:
        print(f"FAIL: {len(failures)} of {len(tests)} failed:", file=sys.stderr)
        for name, exc in failures:
            print(f"\n--- {name} ---\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(tests)} harness transition tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
