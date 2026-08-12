"""Section 5's second invariant: a rejection must cite a rule that exists.

The harness has always overruled the checker in one direction - it will not
commit an action Section 4 forbids. This is the mirror: it will not accept a
REJECTION of an action Section 4 permits.

That is decidable rather than a judgement call because every rule in
transitions.json forbids an *action*; no rule kind can forbid a mood or a line.
So if the rejected action is legal, no rule could have justified the rejection.

Most of these tests are about when the guard must NOT fire. An override that is
too eager would be worse than the bug it fixes: it would silently discard the
checker's genuine work, including the t=60 beat the whole demo is built on.

Run:
    python tests/unit/test_grounded_rejections.py
"""

import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm.harness import Harness, load_config  # noqa: E402
from swarm.state import Event  # noqa: E402

CONFIG, TRANSITIONS = load_config()
TRIGGER = {"type": "event", "event_type": "game_danger", "ts": 60.0}


class FakePersonas:
    """Drives one tick with exactly the proposals and verdict a test needs."""

    def __init__(self, mood, action, verdict, fail=()):
        self.mood, self.action, self.verdict, self.fail = mood, action, verdict, fail

    def mood_picker(self, state, config=None):
        if "mood" in self.fail:
            raise RuntimeError("boom")
        return {"mood": self.mood, "confidence": 0.9, "reason": "test"}

    def action_picker(self, state, mood, config=None):
        if "action" in self.fail:
            raise RuntimeError("boom")
        return {"action": self.action, "confidence": 0.9, "reason": "test"}

    def dialogue_line(self, state, mood, action, config=None):
        return {"line": None, "reason": "test"}

    def transition_checker(self, state, proposals, transitions, config=None):
        if "check" in self.fail:
            raise RuntimeError("boom")
        return dict(self.verdict)


def run_tick(mood, action, verdict, fail=(), *, guard=True,
             current_mood="excited", current_action="celebrate"):
    """One tick with the given proposals; returns the trace record."""
    import swarm.harness as harness_mod
    config = copy.deepcopy(CONFIG)
    config["enforce_grounded_rejections"] = guard
    config["initial_mood"], config["initial_action"] = current_mood, current_action

    harness = Harness(config, TRANSITIONS)
    harness.state.current_mood = current_mood
    harness.state.current_action = current_action
    harness.state.recent_events = [Event("game_danger", 0.7, 60.0)]

    original = harness_mod.persona_impls
    harness_mod.persona_impls = FakePersonas(mood, action, verdict, fail)
    try:
        return harness.run_tick(TRIGGER)
    finally:
        harness_mod.persona_impls = original


def reject(mood, action):
    return {"verdict": "reject", "final_mood": mood, "final_action": action,
            "final_line": None, "reason": "test rejection"}


# --- it fires on an ungrounded rejection --------------------------------------

def test_rejection_of_a_legal_action_is_overruled():
    """`wave` is legal for `alert` - by_mood.alert forbids only celebrate/jump."""
    record = run_tick("alert", "wave", reject("alert", "idle_loop"),
                      current_action="idle_loop")
    assert record["final_state"]["current_action"] == "wave"
    assert record["verdict"]["overruled"]["restored_action"] == "wave"
    assert record["verdict"]["overruled"]["checker_action"] == "idle_loop"


def test_the_checkers_original_verdict_is_preserved_in_the_log():
    """The trace records what the model said, not a tidied version of it."""
    record = run_tick("alert", "wave", reject("alert", "idle_loop"),
                      current_action="idle_loop")
    assert record["verdict"]["verdict"] == "reject"
    assert record["verdict"]["final_action"] == "idle_loop"
    assert record["verdict"]["reason"] == "test rejection"


def test_the_mood_is_never_overruled():
    """Section 4 cannot adjudicate moods, so the harness must not either."""
    record = run_tick("excited", "wave", reject("idle", "idle_loop"),
                      current_action="idle_loop")
    assert record["final_state"]["current_mood"] == "idle"
    assert record["final_state"]["current_action"] == "wave"


def test_legality_is_judged_against_the_committed_mood_not_the_proposed_one():
    """The checker changed the mood to one that DOES forbid the action."""
    # by_mood.angry forbids wave. The checker rejects wave and commits `angry`,
    # so restoring wave would be illegal - the guard must stand down.
    record = run_tick("happy", "wave", reject("angry", "idle_loop"),
                      current_action="idle_loop")
    assert record["final_state"]["current_action"] == "idle_loop"
    assert "overruled" not in record["verdict"]


# --- it stands down when it should -------------------------------------------

def test_a_grounded_rejection_is_honoured():
    """The t=60 beat: look_around genuinely cannot follow celebrate."""
    record = run_tick("alert", "look_around", reject("alert", "idle_loop"),
                      current_action="celebrate")
    assert record["final_state"]["current_action"] == "idle_loop"
    assert "overruled" not in record["verdict"]


def test_an_approval_is_never_touched():
    record = run_tick("alert", "look_around",
                      {"verdict": "approve", "final_mood": "alert",
                       "final_action": "look_around", "final_line": None,
                       "reason": "fine"}, current_action="idle_loop")
    assert record["final_state"]["current_action"] == "look_around"
    assert "overruled" not in record["verdict"]


def test_a_harness_fallback_is_not_second_guessed():
    """The checker FAILED - that is Section 5's last resort, not a judgement."""
    record = run_tick("alert", "wave", reject("alert", "idle_loop"),
                      fail=("check",), current_action="idle_loop")
    assert record["verdict"]["verdict"] == "harness_fallback"
    assert record["final_state"]["current_action"] == "idle_loop"
    assert "overruled" not in record["verdict"]


def test_a_failed_action_picker_leaves_nothing_to_defend():
    """The proposal would be a harness default, not a swarm decision."""
    record = run_tick("alert", "wave", reject("alert", "idle_loop"),
                      fail=("action",), current_action="idle_loop")
    assert record["errors"]["action"] is not None
    assert "overruled" not in record["verdict"]


def test_a_rejection_that_kept_the_action_is_not_an_override():
    """Nothing was actually vetoed, so there is nothing to restore."""
    record = run_tick("alert", "wave", reject("alert", "wave"),
                      current_action="idle_loop")
    assert record["final_state"]["current_action"] == "wave"
    assert "overruled" not in record["verdict"]


def test_the_guard_can_be_switched_off_for_measurement():
    record = run_tick("alert", "wave", reject("alert", "idle_loop"),
                      guard=False, current_action="idle_loop")
    assert record["final_state"]["current_action"] == "idle_loop"
    assert "overruled" not in record["verdict"]


# --- the two invariants compose ----------------------------------------------

def test_the_illegal_commit_invariant_still_wins():
    """An approved illegal action is still substituted - direction one intact."""
    record = run_tick("alert", "celebrate",
                      {"verdict": "approve", "final_mood": "alert",
                       "final_action": "celebrate", "final_line": None,
                       "reason": "wrongly approved"}, current_action="idle_loop")
    assert record["final_state"]["current_action"] == "idle_loop"
    assert "harness invariant" in (record["errors"]["check"] or "")


def test_an_overruled_action_is_always_legal():
    """Whatever the guard restores must survive the invariant below it."""
    for prev in ("idle_loop", "celebrate", "jump", "wave", "duck", "look_around"):
        for act in CONFIG["actions"]:
            record = run_tick("alert", act, reject("alert", "idle_loop"),
                              current_action=prev)
            committed = record["final_state"]["current_action"]
            from swarm.state import banned_actions
            assert committed not in banned_actions(TRANSITIONS, "alert", prev), \
                f"committed {committed!r} is illegal for alert out of {prev!r}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("-" * 60)
    print(f"{'FAIL' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
