"""Phase 2 failure-injection tests (Architecture doc Section 5).

docs/06_TESTING_STRATEGY.md Phase 2 requires proof that a persona failing mid-tick
makes the harness apply the documented fallback, log the failure EXPLICITLY, and
continue to the next tick - rather than crashing, hanging, or silently papering
over it. Silent fallback is the dangerous case: the run looks fine and the trace
lies about why the character did what it did.

Every failure mode is injected by monkeypatching a persona for the duration of
one test. Phase 3 will add real parse/timeout failures on top of these paths; the
harness behaviour asserted here should not change when it does.

    python tests/integration/test_persona_fallbacks.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm import personas as persona_impls  # noqa: E402
from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import load_events  # noqa: E402

CONFIG, TRANSITIONS = load_config()
DEMO_EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))

PERSONA_ATTRS = {
    "mood": "mood_picker",
    "action": "action_picker",
    "line": "dialogue_line",
    "check": "transition_checker",
}


class _Patch:
    """Temporarily replace a persona function."""

    def __init__(self, key, replacement):
        self.attr = PERSONA_ATTRS[key]
        self.replacement = replacement

    def __enter__(self):
        self.original = getattr(persona_impls, self.attr)
        setattr(persona_impls, self.attr, self.replacement)
        return self

    def __exit__(self, *exc):
        setattr(persona_impls, self.attr, self.original)
        return False


def run(events=None, idle_ticks=0, config=None):
    with Harness(config or CONFIG, TRANSITIONS) as h:
        return h, h.run(events if events is not None else DEMO_EVENTS, idle_ticks=idle_ticks)


def config_without_grounded_rejection_guard():
    """Section 5 has two invariants and they overlap on one scenario.

    When the checker rejects a legal action and names an ILLEGAL fallback, the
    grounded-rejection guard fires first and restores the legal proposal, so the
    illegal fallback never reaches the invariant below it. That composition is
    correct and is asserted separately; but a test *of the invariant* has to be
    able to reach the invariant.
    """
    import copy
    config = copy.deepcopy(CONFIG)
    config["enforce_grounded_rejections"] = False
    return config


def boom(*_a, **_kw):
    raise RuntimeError("injected failure")


def assert_run_survived(records, expected_ticks):
    assert len(records) == expected_ticks, f"loop stopped early: {len(records)}/{expected_ticks}"
    assert [r["tick"] for r in records] == list(range(1, expected_ticks + 1))


# --- a raised exception in each persona --------------------------------------

def test_mood_picker_exception_falls_back_and_logs():
    with _Patch("mood", boom):
        _, records = run()
    assert_run_survived(records, len(DEMO_EVENTS))
    for r in records:
        assert r["errors"]["mood"], "the failure must be logged explicitly, not silently swallowed"
        assert "injected failure" in r["errors"]["mood"]
        # Section 5: mood falls back to current_mood.
        assert r["proposals"]["mood"]["mood"] == r["input_state"]["current_mood"]


def test_action_picker_exception_falls_back_to_idle_loop():
    with _Patch("action", boom):
        _, records = run()
    assert_run_survived(records, len(DEMO_EVENTS))
    for r in records:
        assert r["errors"]["action"]
        assert r["proposals"]["action"]["action"] == CONFIG["fallback"]["action"] == "idle_loop"


def test_dialogue_line_exception_falls_back_to_null():
    with _Patch("line", boom):
        _, records = run()
    assert_run_survived(records, len(DEMO_EVENTS))
    for r in records:
        assert r["errors"]["line"]
        assert r["proposals"]["line"]["line"] is None


def test_transition_checker_exception_triggers_harness_level_fallback():
    with _Patch("check", boom):
        _, records = run()
    assert_run_survived(records, len(DEMO_EVENTS))
    for r in records:
        assert r["errors"]["check"]
        # Distinguishable from an ordinary reject, per Section 6.
        assert r["verdict"]["verdict"] == "harness_fallback"
        assert r["verdict"]["final_action"] == "idle_loop"


def test_all_four_personas_failing_still_completes_the_run():
    with _Patch("mood", boom), _Patch("action", boom), _Patch("line", boom), _Patch("check", boom):
        harness, records = run(idle_ticks=2)
    assert_run_survived(records, len(DEMO_EVENTS) + 2)
    # The character holds a safe, legal state rather than the loop dying.
    assert harness.state.current_action == "idle_loop"
    for r in records:
        assert all(r["errors"][k] for k in ("mood", "action", "line", "check"))


# --- malformed-but-not-raising output ----------------------------------------

def test_out_of_enum_mood_is_treated_as_a_failure():
    """Section 5, clarified 2026-07-30: valid-shaped output carrying a value
    outside the enum is a persona FAILURE, not a usable proposal."""
    with _Patch("mood", lambda *a, **k: {"mood": "neutral", "confidence": 0.9, "reason": "x"}):
        _, records = run()
    for r in records:
        assert r["errors"]["mood"] and "out_of_enum" in r["errors"]["mood"]
        assert r["final_state"]["current_mood"] in CONFIG["moods"]


def test_out_of_enum_action_is_treated_as_a_failure():
    with _Patch("action", lambda *a, **k: {"action": "moonwalk", "confidence": 0.9, "reason": "x"}):
        _, records = run()
    for r in records:
        assert r["errors"]["action"] and "out_of_enum" in r["errors"]["action"]
        assert r["final_state"]["current_action"] in CONFIG["actions"]


def test_missing_key_is_treated_as_a_failure():
    with _Patch("mood", lambda *a, **k: {"confidence": 0.9, "reason": "no mood key"}):
        _, records = run()
    for r in records:
        assert r["errors"]["mood"] and "missing key" in r["errors"]["mood"]


def test_non_dict_response_is_treated_as_a_failure():
    with _Patch("mood", lambda *a, **k: "excited"):
        _, records = run()
    for r in records:
        assert r["errors"]["mood"] and "not an object" in r["errors"]["mood"]


def test_none_response_is_treated_as_a_failure():
    with _Patch("line", lambda *a, **k: None):
        _, records = run()
    for r in records:
        assert r["errors"]["line"]


def test_checker_returning_a_bad_verdict_is_a_failure():
    with _Patch("check", lambda *a, **k: {
        "verdict": "maybe", "final_mood": "idle", "final_action": "idle_loop", "reason": "x"
    }):
        _, records = run()
    for r in records:
        assert r["errors"]["check"] and "bad verdict" in r["errors"]["check"]
        assert r["verdict"]["verdict"] == "harness_fallback"


def test_checker_inventing_an_action_is_a_failure():
    with _Patch("check", lambda *a, **k: {
        "verdict": "reject", "final_mood": "alert", "final_action": "freeze", "reason": "x"
    }):
        _, records = run()
    for r in records:
        assert r["errors"]["check"] and "out_of_enum" in r["errors"]["check"]
        assert r["final_state"]["current_action"] in CONFIG["actions"]


# --- the deterministic invariant behind the checker --------------------------

def test_harness_invariant_blocks_an_illegal_but_in_enum_fallback():
    """Section 5's invariant.

    A checker can return a perfectly well-formed verdict that is still illegal:
    `jump` is in the enum but disallowed for `alert`. Phase 0 measured real
    models doing exactly this. The harness must refuse to commit it regardless of
    what the checker said, and must say so in the trace.
    """
    with _Patch("check", lambda *a, **k: {
        "verdict": "reject", "final_mood": "alert", "final_action": "jump",
        "final_line": None, "reason": "illegal fallback",
    }):
        # Guard off, so this exercises the invariant itself rather than the
        # grounded-rejection guard that now sits in front of it.
        _, records = run(config=config_without_grounded_rejection_guard())

    banned = TRANSITIONS["by_mood"]["alert"]["disallowed_next_action"]
    assert "jump" in banned, "fixture assumption: jump must be disallowed for alert"
    for r in records:
        assert r["final_state"]["current_action"] not in banned
        assert "harness invariant" in (r["errors"]["check"] or ""), (
            "the substitution must be recorded, or the trace would misreport why "
            "the character did what it did"
        )


def test_the_two_invariants_compose_without_a_gap():
    """Same injection, guard ON: still no illegal commit, by a different route.

    This is the case both invariants can claim. The guard reaches it first -
    the rejected action was legal, so the rejection cited no rule - and restores
    the action-picker's proposal, which means the illegal `jump` never gets far
    enough for the invariant to substitute. What must hold either way is the
    property, not which mechanism enforced it: nothing illegal is committed, and
    the trace says which one acted.
    """
    with _Patch("check", lambda *a, **k: {
        "verdict": "reject", "final_mood": "alert", "final_action": "jump",
        "final_line": None, "reason": "illegal fallback",
    }):
        _, records = run()

    banned = TRANSITIONS["by_mood"]["alert"]["disallowed_next_action"]
    for r in records:
        assert r["final_state"]["current_action"] not in banned
        acted = ("overruled" in r["verdict"]) or ("harness invariant" in (r["errors"]["check"] or ""))
        assert acted, f"tick {r['tick']}: illegal fallback neither overruled nor substituted"
    assert any("overruled" in r["verdict"] for r in records), (
        "with the guard on, at least one tick should be handled by it"
    )


def test_invariant_records_the_substitution_even_when_the_checker_succeeded():
    with _Patch("check", lambda *a, **k: {
        "verdict": "approve", "final_mood": "sad", "final_action": "celebrate",
        "final_line": None, "reason": "wrongly approved",
    }):
        _, records = run()
    for r in records:
        assert r["final_state"]["current_action"] == "idle_loop"
        assert "harness invariant" in (r["errors"]["check"] or "")


# --- failure isolation -------------------------------------------------------

def test_one_persona_failing_does_not_mark_the_others_failed():
    with _Patch("line", boom):
        _, records = run()
    for r in records:
        assert r["errors"]["line"]
        assert r["errors"]["mood"] is None
        assert r["errors"]["action"] is None
        assert r["errors"]["check"] is None


def test_a_transient_failure_does_not_poison_later_ticks():
    """A persona that fails once must not leave the harness degraded."""
    calls = {"n": 0}
    original = persona_impls.mood_picker

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected failure")
        return original(*args, **kwargs)

    with _Patch("mood", flaky):
        _, records = run()

    assert records[1]["errors"]["mood"], "tick 2 should have failed"
    assert all(r["errors"]["mood"] is None for i, r in enumerate(records) if i != 1)


def test_timings_are_still_recorded_for_a_failed_persona():
    with _Patch("mood", boom):
        _, records = run()
    for r in records:
        assert r["timing_ms"]["mood"] >= 0


def test_trace_still_has_the_full_section_6_shape_under_failure():
    with _Patch("check", boom):
        _, records = run()
    expected = {"tick", "config_version", "trigger", "input_state", "proposals",
                "errors", "verdict", "final_state", "timing_ms"}
    for r in records:
        assert set(r) == expected


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
    print(f"OK: {len(tests)} fallback tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
