"""Phase 2 integration tests for swarm/harness.py (Architecture doc Sections 1 and 6).

docs/06_TESTING_STRATEGY.md Phase 2 asks for:
  - the four personas called in the documented order, each seeing the prior
    outputs, asserted with a call-order spy rather than inferred from the result
  - trace log: every line valid JSON, exactly the Section 6 keys, tick numbers
    sequential with no gaps or repeats
  - edge cases: a zero-event run, and a 500+ event stress run that completes with
    bounded memory and log growth

Uses the real config so the tests fail if config and code drift apart. No model
calls anywhere - Phase 2 personas are mocks.

    python tests/integration/test_harness_loop.py
"""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm import harness as harness_mod  # noqa: E402
from swarm import personas as persona_impls  # noqa: E402
from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import Event, load_events  # noqa: E402

CONFIG, TRANSITIONS = load_config()
DEMO_EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))

SECTION_6_KEYS = {
    "tick", "config_version", "trigger", "input_state", "proposals",
    "errors", "verdict", "final_state", "timing_ms",
}
PERSONA_KEYS = {"mood", "action", "line", "check"}


def run_harness(events, **kwargs):
    with Harness(CONFIG, TRANSITIONS) as h:
        return h, h.run(events, **kwargs)


# --- Section 1: call order and information flow ------------------------------

def test_personas_are_called_in_the_documented_order():
    calls = []
    original = (
        persona_impls.mood_picker, persona_impls.action_picker,
        persona_impls.dialogue_line, persona_impls.transition_checker,
    )

    def spy(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapper

    persona_impls.mood_picker = spy("mood", original[0])
    persona_impls.action_picker = spy("action", original[1])
    persona_impls.dialogue_line = spy("line", original[2])
    persona_impls.transition_checker = spy("check", original[3])
    try:
        run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    finally:
        (persona_impls.mood_picker, persona_impls.action_picker,
         persona_impls.dialogue_line, persona_impls.transition_checker) = original

    assert calls == ["mood", "action", "line", "check"], calls


def test_action_picker_sees_the_proposed_mood():
    seen = {}
    original = persona_impls.action_picker

    def spy(state, proposed_mood, *a, **kw):
        seen["mood"] = proposed_mood
        return original(state, proposed_mood, *a, **kw)

    persona_impls.action_picker = spy
    try:
        _, records = run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    finally:
        persona_impls.action_picker = original

    assert seen["mood"]["mood"] == records[0]["proposals"]["mood"]["mood"]


def test_dialogue_line_sees_both_mood_and_action():
    seen = {}
    original = persona_impls.dialogue_line

    def spy(state, proposed_mood, proposed_action, *a, **kw):
        seen["mood"], seen["action"] = proposed_mood, proposed_action
        return original(state, proposed_mood, proposed_action, *a, **kw)

    persona_impls.dialogue_line = spy
    try:
        _, records = run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    finally:
        persona_impls.dialogue_line = original

    assert seen["mood"]["mood"] == records[0]["proposals"]["mood"]["mood"]
    assert seen["action"]["action"] == records[0]["proposals"]["action"]["action"]


def test_transition_checker_sees_all_three_proposals():
    seen = {}
    original = persona_impls.transition_checker

    def spy(state, proposals, transitions, *a, **kw):
        seen.update(proposals)
        return original(state, proposals, transitions, *a, **kw)

    persona_impls.transition_checker = spy
    try:
        run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    finally:
        persona_impls.transition_checker = original

    assert set(seen) == {"mood", "action", "line"}


# --- Section 6: trace log ----------------------------------------------------

def test_trace_file_has_one_valid_json_line_per_tick():
    with TemporaryDirectory() as tmp:
        trace = Path(tmp) / "trace.jsonl"
        with Harness(CONFIG, TRANSITIONS, trace_path=trace) as h:
            records = h.run(DEMO_EVENTS, idle_ticks=2)
        lines = trace.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(records), f"{len(lines)} lines for {len(records)} ticks"
    for line in lines:
        json.loads(line)  # raises if any line is not valid standalone JSON


def test_every_trace_line_has_exactly_the_section_6_keys():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    for r in records:
        assert set(r) == SECTION_6_KEYS, f"tick {r['tick']}: {set(r) ^ SECTION_6_KEYS}"
        assert set(r["proposals"]) == {"mood", "action", "line"}
        assert set(r["errors"]) == PERSONA_KEYS
        assert set(r["timing_ms"]) == PERSONA_KEYS
        assert set(r["trigger"]) == {"type", "event_type", "ts"}


def test_persona_name_to_error_key_mapping_is_complete():
    """The config says "checker"; the trace says "check". Both are load-bearing.

    This bit for real on 2026-08-11: code that counted failures with
    `errors.get(name)` silently missed every checker failure, because
    `errors["checker"]` does not exist and `.get` returns None rather than
    raising. The run then looked 30/40 instead of 40/40 and the "no model
    reachable" diagnostic never fired. Any new persona must appear in both
    vocabularies or the same silence returns.
    """
    from swarm.harness import ERROR_KEY_FOR, PERSONA_NAMES
    from swarm.harness import PERSONA_KEYS as CODE_KEYS

    assert set(ERROR_KEY_FOR) == set(PERSONA_NAMES), "a persona has no error-key mapping"
    assert set(ERROR_KEY_FOR.values()) == set(CODE_KEYS), "mapping does not cover the trace keys"
    assert len(set(ERROR_KEY_FOR.values())) == len(ERROR_KEY_FOR), "two personas share an error key"

    # And the mapping must agree with a real trace, not just with itself.
    _, records = run_harness(DEMO_EVENTS, idle_ticks=1)
    for name in PERSONA_NAMES:
        assert ERROR_KEY_FOR[name] in records[0]["errors"]


def test_tick_numbers_are_sequential_with_no_gaps_or_repeats():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    assert [r["tick"] for r in records] == list(range(1, len(records) + 1))


def test_trigger_distinguishes_event_from_timer_ticks():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    kinds = [r["trigger"]["type"] for r in records]
    assert kinds[:len(DEMO_EVENTS)] == ["event"] * len(DEMO_EVENTS)
    assert kinds[len(DEMO_EVENTS):] == ["timer", "timer"]
    for r in records:
        if r["trigger"]["type"] == "timer":
            assert r["trigger"]["event_type"] is None


def test_input_state_is_the_state_before_the_tick_committed():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=0)
    for prev, cur in zip(records, records[1:]):
        assert cur["input_state"]["current_mood"] == prev["final_state"]["current_mood"]
        assert cur["input_state"]["current_action"] == prev["final_state"]["current_action"]


def test_timings_are_recorded_for_all_four_personas():
    _, records = run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    for value in records[0]["timing_ms"].values():
        assert isinstance(value, (int, float)) and value >= 0


# --- Section 1 behaviour over the real demo sequence -------------------------

def test_full_demo_sequence_runs_without_crashing():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    assert len(records) == len(DEMO_EVENTS) + 2


def test_no_persona_failures_on_a_clean_mocked_run():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    problems = [(r["tick"], r["errors"]) for r in records if any(r["errors"].values())]
    assert not problems, problems


def test_the_t60_tick_produces_a_visible_override():
    """Demo Script Acceptance Criteria, Functional item 3.

    Phase 2 cannot prove a real model does this, but it must prove the loop and
    the trace format can REPRESENT it - otherwise Phase 4 would discover the gap
    at the worst possible moment.
    """
    _, records = run_harness(DEMO_EVENTS, idle_ticks=0)
    t60 = [r for r in records if r["trigger"]["ts"] == 60.0]
    assert len(t60) == 1
    record = t60[0]
    assert record["verdict"]["verdict"] == "reject"
    # The character is mid-celebrate and the action-picker correctly proposes a
    # mood-appropriate action; the SMOOTHNESS rule (by_previous_action) is what
    # makes it illegal, not the action-picker misbehaving (Section 4).
    assert record["input_state"]["current_action"] == "celebrate"
    proposed = record["proposals"]["action"]["action"]
    assert proposed in TRANSITIONS["by_previous_action"]["celebrate"]["disallowed_next_action"], proposed
    assert record["verdict"]["final_action"] != proposed
    assert record["final_state"]["current_action"] == "idle_loop"


def test_committed_state_never_violates_the_transition_table():
    """Section 5's invariant, over every tick of the real sequence."""
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    from swarm.state import banned_actions
    for r in records:
        mood = r["final_state"]["current_mood"]
        action = r["final_state"]["current_action"]
        previous = r["input_state"]["current_action"]
        banned = banned_actions(TRANSITIONS, mood, previous)
        assert action not in banned, f"tick {r['tick']}: committed {mood}/{action} after {previous}"


def test_committed_values_are_always_in_enum():
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    for r in records:
        assert r["final_state"]["current_mood"] in CONFIG["moods"]
        assert r["final_state"]["current_action"] in CONFIG["actions"]


def test_run_ends_in_a_stable_idle_state():
    # Demo Script Acceptance Criteria, Functional item 5.
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    assert records[-1]["final_state"]["current_mood"] == "idle"
    assert records[-1]["final_state"]["current_action"] == "idle_loop"


def test_three_distinct_moods_and_actions_appear():
    # Acceptance Criteria, Functional item 2.
    _, records = run_harness(DEMO_EVENTS, idle_ticks=2)
    moods = {r["final_state"]["current_mood"] for r in records}
    actions = {r["final_state"]["current_action"] for r in records}
    assert len(moods) >= 3, moods
    assert len(actions) >= 3, actions


def test_redundant_chat_calm_at_t15_changes_nothing():
    # Acceptance Criteria, Functional item 4: the system must not react to every
    # input blindly. At t=15 the character is already idle/idle_loop.
    _, records = run_harness(DEMO_EVENTS, idle_ticks=0)
    first = records[0]
    assert first["trigger"]["ts"] == 15.0
    assert first["final_state"]["current_mood"] == "idle"
    assert first["final_state"]["current_action"] == "idle_loop"


# --- step 7: directives ------------------------------------------------------

def test_a_directive_is_emitted_per_tick_in_the_contract_shape():
    harness, records = run_harness(DEMO_EVENTS, idle_ticks=1)
    assert len(harness.directives) == len(records)
    for d, r in zip(harness.directives, records):
        assert set(d) == {"tick", "mood", "action", "line", "ts"}
        assert d["mood"] == r["final_state"]["current_mood"]
        assert d["action"] == r["final_state"]["current_action"]


def test_a_sink_receives_every_directive():
    received = []
    with Harness(CONFIG, TRANSITIONS, sink=received.append) as h:
        records = h.run(DEMO_EVENTS, idle_ticks=1)
    assert len(received) == len(records)


# --- edge cases --------------------------------------------------------------

def test_zero_event_run_completes_cleanly():
    # 06 Phase 2 edge case: the harness must run zero ticks rather than crash.
    harness, records = run_harness([], idle_ticks=0)
    assert records == []
    assert harness.directives == []


def test_zero_events_with_idle_ticks_still_ticks():
    _, records = run_harness([], idle_ticks=3)
    assert len(records) == 3
    assert all(r["trigger"]["type"] == "timer" for r in records)


def test_single_event_run():
    _, records = run_harness(DEMO_EVENTS[:1], idle_ticks=0)
    assert len(records) == 1


def test_stress_run_of_500_events_stays_bounded():
    """06 Phase 2 edge case: memory and log growth must stay bounded, i.e. the
    rolling window really rolls rather than silently accumulating."""
    events = [
        Event(type="chat_hype_spike" if i % 2 else "game_danger",
              intensity=0.5, ts=float(i * 2))
        for i in range(500)
    ]
    with TemporaryDirectory() as tmp:
        trace = Path(tmp) / "stress.jsonl"
        with Harness(CONFIG, TRANSITIONS, trace_path=trace) as h:
            records = h.run(events, idle_ticks=0)
            assert len(h.state.recent_events) <= CONFIG["recent_events_max_count"]
        lines = trace.read_text(encoding="utf-8").strip().splitlines()

    assert len(records) == 500
    assert len(lines) == 500
    for r in records:
        assert len(r["input_state"]["recent_events"]) <= CONFIG["recent_events_max_count"]


def test_events_sharing_a_timestamp_both_produce_ticks():
    events = [
        Event(type="chat_hype_spike", intensity=0.6, ts=30.0),
        Event(type="game_danger", intensity=0.7, ts=30.0),
    ]
    _, records = run_harness(events, idle_ticks=0)
    assert len(records) == 2
    assert [r["trigger"]["event_type"] for r in records] == ["chat_hype_spike", "game_danger"]


def test_simulated_clock_run_is_fast():
    # The default must not sleep - a 130s demo sequence would make the suite
    # unusable. --realtime exists for actual demo runs.
    import time as _time
    started = _time.perf_counter()
    run_harness(DEMO_EVENTS, idle_ticks=2)
    assert _time.perf_counter() - started < 5.0


# --- CLI ---------------------------------------------------------------------

def test_cli_writes_a_trace_and_exits_zero():
    with TemporaryDirectory() as tmp:
        trace = Path(tmp) / "cli.jsonl"
        code = harness_mod.main(["--trace", str(trace), "--quiet"])
        assert code == 0
        lines = trace.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(DEMO_EVENTS) + 2


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
    print(f"OK: {len(tests)} harness loop tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
