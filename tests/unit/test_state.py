"""Phase 2 unit tests for swarm/state.py (Architecture doc Section 2).

docs/06_TESTING_STRATEGY.md Phase 2 asks specifically for: default values, the
recent_events window bounded correctly at exactly N, N-1 and N+1 events, and
ticks_since_last_change incrementing and resetting correctly.

Deterministic and fast - no model calls, no filesystem, no clock.

    python tests/unit/test_state.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm.state import (  # noqa: E402
    DEFAULT_ACTION,
    DEFAULT_MOOD,
    Event,
    SwarmState,
    banned_actions,
    is_legal,
    legal_actions,
    load_events,
    state_from_config,
)


def ev(type_="chat_calm", intensity=1.0, ts=0.0, **meta):
    return Event(type=type_, intensity=intensity, ts=ts, meta=meta)


# --- defaults ----------------------------------------------------------------

def test_defaults_match_section_2():
    s = SwarmState()
    assert s.current_mood == DEFAULT_MOOD == "idle"
    assert s.current_action == DEFAULT_ACTION == "idle_loop"
    assert s.last_line is None
    assert s.ticks_since_last_change == 0
    assert s.recent_events == []


def test_prompt_dict_has_exactly_the_section_2_keys():
    s = SwarmState()
    assert set(s.to_prompt_dict()) == {
        "current_mood", "current_action", "last_line",
        "ticks_since_last_change", "recent_events",
    }


def test_event_prompt_dict_drops_meta():
    # Section 2's example shows type/intensity/ts only; meta is harness routing
    # information and would just enlarge the prompt.
    s = SwarmState()
    s.add_event(ev(ts=1.0, source="scripted_demo"))
    assert set(s.to_prompt_dict()["recent_events"][0]) == {"type", "intensity", "ts"}


def test_state_from_config_reads_window_bounds():
    s = state_from_config({"recent_events_max_count": 3, "recent_events_max_age_s": 30})
    assert s.max_events == 3
    assert s.max_age_s == 30.0


def test_state_from_config_falls_back_to_defaults():
    s = state_from_config({})
    assert s.max_events == 5
    assert s.max_age_s == 60.0


# --- window bounds: count (N-1, N, N+1) --------------------------------------

def test_window_below_capacity_keeps_all():
    s = SwarmState(max_events=5, max_age_s=None)
    for i in range(4):                      # N-1
        s.add_event(ev(ts=float(i)))
    assert len(s.recent_events) == 4


def test_window_at_capacity_keeps_all():
    s = SwarmState(max_events=5, max_age_s=None)
    for i in range(5):                      # exactly N
        s.add_event(ev(ts=float(i)))
    assert len(s.recent_events) == 5


def test_window_over_capacity_drops_oldest():
    s = SwarmState(max_events=5, max_age_s=None)
    for i in range(6):                      # N+1
        s.add_event(ev(ts=float(i)))
    assert len(s.recent_events) == 5
    assert [e.ts for e in s.recent_events] == [1.0, 2.0, 3.0, 4.0, 5.0], "the OLDEST must be dropped"


def test_window_stays_bounded_under_many_events():
    # The rolling window must actually roll - an unbounded list would make
    # prompts grow without limit over a long session.
    s = SwarmState(max_events=5, max_age_s=None)
    for i in range(500):
        s.add_event(ev(ts=float(i)))
    assert len(s.recent_events) == 5
    assert s.recent_events[-1].ts == 499.0


# --- window bounds: age ------------------------------------------------------

def test_events_older_than_max_age_are_dropped():
    s = SwarmState(max_events=99, max_age_s=60.0)
    s.add_event(ev(ts=10.0))
    s.add_event(ev(ts=100.0))               # now 90s later; the first is stale
    assert [e.ts for e in s.recent_events] == [100.0]


def test_event_exactly_at_the_age_boundary_is_kept():
    # Boundary: at now=75 with a 60s window, a t=15 event is exactly 60s old.
    s = SwarmState(max_events=99, max_age_s=60.0)
    s.add_event(ev(ts=15.0))
    s.prune(75.0)
    assert [e.ts for e in s.recent_events] == [15.0]


def test_event_one_second_past_the_boundary_is_dropped():
    s = SwarmState(max_events=99, max_age_s=60.0)
    s.add_event(ev(ts=15.0))
    s.prune(76.0)
    assert s.recent_events == []


def test_count_and_age_bounds_both_apply():
    # Whichever binds first wins: 6 events inside the age window, capped to 5.
    s = SwarmState(max_events=5, max_age_s=60.0)
    for ts in (50.0, 52.0, 54.0, 56.0, 58.0, 60.0):
        s.add_event(ev(ts=ts))
    assert len(s.recent_events) == 5
    assert [e.ts for e in s.recent_events] == [52.0, 54.0, 56.0, 58.0, 60.0]


def test_the_t60_window_still_holds_the_hype_events():
    """The window must not erase the t=60 conflict.

    Architecture doc Section 2 chose 5 events / 60s specifically so that at t=60
    the celebratory momentum (both chat_hype_spike events) is still visible
    alongside the new game_danger. If this ever fails, the demo's headline beat
    disappears for an uninteresting reason.
    """
    s = SwarmState(max_events=5, max_age_s=60.0)
    for ts, kind in ((15.0, "chat_calm"), (30.0, "chat_hype_spike"),
                     (45.0, "chat_hype_spike"), (60.0, "game_danger")):
        s.add_event(ev(kind, ts=ts))
    kinds = [e.type for e in s.recent_events]
    assert kinds.count("chat_hype_spike") == 2, kinds
    assert "game_danger" in kinds


# --- ticks_since_last_change -------------------------------------------------

def test_counter_increments_when_nothing_changes():
    s = SwarmState()
    assert s.commit("idle", "idle_loop", None) is False
    assert s.ticks_since_last_change == 1
    s.commit("idle", "idle_loop", None)
    assert s.ticks_since_last_change == 2


def test_counter_resets_when_mood_changes():
    s = SwarmState()
    s.commit("idle", "idle_loop", None)
    s.commit("idle", "idle_loop", None)
    assert s.ticks_since_last_change == 2
    assert s.commit("excited", "idle_loop", None) is True
    assert s.ticks_since_last_change == 0


def test_counter_resets_when_action_changes():
    s = SwarmState()
    s.commit("idle", "idle_loop", None)
    assert s.commit("idle", "wave", None) is True
    assert s.ticks_since_last_change == 0


def test_a_line_alone_does_not_reset_the_counter():
    # Lines are transient (Section 3.3 biases toward silence); counting them as
    # "change" would pin the counter near zero and make it useless.
    s = SwarmState()
    s.commit("idle", "idle_loop", None)
    assert s.commit("idle", "idle_loop", "Hello!") is False
    assert s.ticks_since_last_change == 2
    assert s.last_line == "Hello!"


# --- snapshot isolation ------------------------------------------------------

def test_snapshot_is_independent_of_later_mutation():
    # The logged input_state of a tick must not be mutated by the rest of it.
    s = SwarmState()
    s.add_event(ev(ts=1.0))
    snap = s.snapshot()
    s.add_event(ev(ts=2.0))
    s.commit("excited", "celebrate", "hi")
    assert len(snap.recent_events) == 1
    assert snap.current_mood == "idle"


# --- event loading -----------------------------------------------------------

def test_load_events_sorts_by_ts():
    events = load_events([
        {"type": "chat_calm", "intensity": 1.0, "ts": 30},
        {"type": "game_safe", "intensity": 1.0, "ts": 10},
    ])
    assert [e.ts for e in events] == [10.0, 30.0]


def test_load_events_is_a_stable_sort_for_equal_ts():
    # Interface Contract Section 2.1: ties keep file order.
    events = load_events([
        {"type": "chat_calm", "intensity": 1.0, "ts": 10},
        {"type": "game_safe", "intensity": 1.0, "ts": 10},
        {"type": "game_danger", "intensity": 0.5, "ts": 10},
    ])
    assert [e.type for e in events] == ["chat_calm", "game_safe", "game_danger"]


def test_load_events_accepts_an_empty_list():
    assert load_events([]) == []


def test_event_from_dict_coerces_numeric_types():
    e = Event.from_dict({"type": "chat_calm", "intensity": 1, "ts": 15})
    assert isinstance(e.intensity, float) and isinstance(e.ts, float)


def test_event_from_dict_tolerates_missing_meta():
    assert Event.from_dict({"type": "chat_calm", "intensity": 1.0, "ts": 1.0}).meta == {}


# --- transition rules (Architecture doc Section 4) ---------------------------
# These guard the shared helper the harness, the mock checker and the validator
# all delegate to. A hand-rolled lookup that indexed TRANSITIONS by mood used to
# live in a test; under the two-section schema it silently returned {} and made
# that assertion vacuous. One implementation, directly covered, prevents that.

RULES = {
    "by_mood": {
        "sad": {"disallowed_next_action": ["celebrate", "jump"]},
        "alert": {"disallowed_next_action": ["celebrate", "jump"]},
    },
    "by_previous_action": {
        "celebrate": {"disallowed_next_action": ["duck", "look_around"]},
    },
    "_comment": "documentation key, must be ignored",
}
ALL_ACTIONS = ["idle_loop", "wave", "jump", "duck", "celebrate", "look_around"]


def test_by_mood_rule_alone():
    assert banned_actions(RULES, "alert", "idle_loop") == {"celebrate", "jump"}


def test_by_previous_action_rule_alone():
    assert banned_actions(RULES, "happy", "celebrate") == {"duck", "look_around"}


def test_both_rules_combine():
    # This is the t=60 situation: alert mood, still mid-celebrate.
    assert banned_actions(RULES, "alert", "celebrate") == {
        "celebrate", "jump", "duck", "look_around"
    }


def test_no_rules_bans_nothing():
    assert banned_actions(RULES, "happy", "idle_loop") == set()


def test_unknown_keys_ban_nothing_rather_than_raising():
    assert banned_actions(RULES, "nonexistent", "nonexistent") == set()
    assert banned_actions(RULES, "alert", None) == {"celebrate", "jump"}


def test_documentation_keys_are_not_mistaken_for_rules():
    assert banned_actions(RULES, "_comment", "_comment") == set()


def test_indexing_by_mood_at_top_level_finds_nothing():
    """The exact bug this helper exists to prevent.

    Under the two-section schema a top-level mood lookup returns {}, so a
    hand-rolled check would pass no matter what was committed.
    """
    assert RULES.get("alert", {}).get("disallowed_next_action", []) == []
    assert banned_actions(RULES, "alert", "celebrate"), "the helper must still find the rules"


def test_is_legal_agrees_with_banned_actions():
    for mood in ("alert", "happy"):
        for prev in ALL_ACTIONS:
            banned = banned_actions(RULES, mood, prev)
            for action in ALL_ACTIONS:
                assert is_legal(RULES, mood, prev, action) == (action not in banned)


def test_legal_actions_excludes_exactly_the_banned_ones():
    legal = legal_actions(RULES, "alert", "celebrate", ALL_ACTIONS)
    assert legal == ["idle_loop", "wave"]


def test_legal_actions_preserves_enum_order():
    assert legal_actions(RULES, "happy", "idle_loop", ALL_ACTIONS) == ALL_ACTIONS


def test_the_fallback_action_survives_every_combination():
    """Section 5 pins idle_loop as the always-committable fallback."""
    for mood in list(RULES["by_mood"]) + ["happy", "idle"]:
        for prev in ALL_ACTIONS + [None]:
            assert "idle_loop" in legal_actions(RULES, mood, prev, ALL_ACTIONS), (mood, prev)


def test_committed_transitions_table_leaves_the_fallback_legal():
    """Same guarantee, against the real config rather than a fixture."""
    import json
    real = json.loads((REPO_ROOT / "config" / "transitions.json").read_text(encoding="utf-8-sig"))
    actions = json.loads(
        (REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8-sig")
    )["actions"]
    moods = json.loads(
        (REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8-sig")
    )["moods"]
    for mood in moods:
        for prev in actions + [None]:
            assert "idle_loop" in legal_actions(real, mood, prev, actions), (mood, prev)


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
    print(f"OK: {len(tests)} state tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
