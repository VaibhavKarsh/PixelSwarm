"""Structural regression guard against the golden trace (06 Section 2.3).

06 asks for a known-good trace saved after Phase 2 and diffed against on later
runs — comparing STRUCTURE, not content, since model output is not byte
reproducible. It is the regression net for Phase 6 prompt tuning specifically:
it catches "fixed the parse failures but now nothing ever gets angry", which no
single-run eyeball would notice.

The fixture is the mocked run, which IS deterministic, so this file can assert
content exactly. Phase 4 adds a second fixture from a real run, where only the
structural assertions can apply.

One field is deliberately NOT asserted on: `config_version`. Mocked personas
never read a prompt, so a prompt edit cannot change mocked behaviour — requiring
the fixture to be regenerated for one would be pure churn. The value is kept as
provenance (it records which config produced the fixture) and only its presence
is checked.

Regenerate deliberately, never casually:
    python -m swarm.harness --quiet --trace tests/fixtures/golden_trace_mocked.jsonl

    python tests/integration/test_golden_trace.py
"""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import banned_actions, load_events  # noqa: E402

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden_trace_mocked.jsonl"
CONFIG, TRANSITIONS = load_config()
DEMO_EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))

SECTION_6_KEYS = {
    "tick", "config_version", "trigger", "input_state", "proposals",
    "errors", "verdict", "final_state", "timing_ms",
}


def golden():
    assert GOLDEN.is_file(), f"missing fixture: {GOLDEN}. Regenerate per this file's docstring."
    return [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def fresh():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        with Harness(CONFIG, TRANSITIONS, trace_path=path) as h:
            h.run(DEMO_EVENTS, idle_ticks=2)
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- the fixture itself is well-formed ---------------------------------------

def test_golden_is_valid_jsonl_with_the_section_6_shape():
    for record in golden():
        assert set(record) == SECTION_6_KEYS, record.get("tick")


def test_golden_has_one_line_per_tick_numbered_sequentially():
    records = golden()
    assert [r["tick"] for r in records] == list(range(1, len(records) + 1))


# --- structural equivalence (what survives non-determinism) ------------------

def test_tick_count_matches():
    assert len(fresh()) == len(golden())


def test_trigger_sequence_matches():
    assert [r["trigger"]["event_type"] for r in fresh()] == \
           [r["trigger"]["event_type"] for r in golden()]


def test_key_sets_match_record_for_record():
    for new, old in zip(fresh(), golden()):
        assert set(new) == set(old)
        assert set(new["errors"]) == set(old["errors"])
        assert set(new["timing_ms"]) == set(old["timing_ms"])
        assert set(new["proposals"]) == set(old["proposals"])


# --- the decision points that must not silently regress ----------------------

def test_the_override_still_happens_at_the_same_tick():
    """The Phase 6 tuning trap: a change that quietly removes the demo's beat."""
    new_overrides = [r["tick"] for r in fresh() if r["verdict"]["verdict"] != "approve"]
    old_overrides = [r["tick"] for r in golden() if r["verdict"]["verdict"] != "approve"]
    assert new_overrides == old_overrides, f"override ticks moved: {old_overrides} -> {new_overrides}"


def test_mood_and_action_coverage_has_not_shrunk():
    """Catches 'nothing ever gets angry any more' — invisible in a single run."""
    new, old = fresh(), golden()
    for field in ("current_mood", "current_action"):
        new_values = {r["final_state"][field] for r in new}
        old_values = {r["final_state"][field] for r in old}
        assert new_values >= old_values, f"{field} coverage shrank: lost {old_values - new_values}"


def test_final_state_sequence_matches_exactly():
    # The mocked run is deterministic, so any difference here is a real change.
    for new, old in zip(fresh(), golden()):
        assert new["final_state"]["current_mood"] == old["final_state"]["current_mood"], new["tick"]
        assert new["final_state"]["current_action"] == old["final_state"]["current_action"], new["tick"]


def test_no_persona_failures_in_the_golden_run():
    for record in golden():
        assert not any(record["errors"].values()), (record["tick"], record["errors"])


def test_every_committed_state_in_the_golden_run_is_legal():
    for record in golden():
        mood = record["final_state"]["current_mood"]
        action = record["final_state"]["current_action"]
        previous = record["input_state"]["current_action"]
        assert action not in banned_actions(TRANSITIONS, mood, previous), record["tick"]


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
        print(
            "\nIf the change was intentional, regenerate the fixture:\n"
            "  python -m swarm.harness --quiet --trace tests/fixtures/golden_trace_mocked.jsonl",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {len(tests)} golden trace tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
