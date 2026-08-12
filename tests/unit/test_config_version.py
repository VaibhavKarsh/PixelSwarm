"""The config fingerprint stamped on every trace record (Architecture Section 6).

A fingerprint is only worth having if it satisfies two opposite properties, and
both are easy to get wrong in the same direction:

  - it must CHANGE when something behavioural changes (otherwise two
    incomparable runs look identical - the Phase 6 stale-trace mistake), and
  - it must NOT change when nothing behavioural changed (otherwise it is noise
    and people stop reading it).

Prompt text is the interesting case. Prompts live in code, not in config/, so a
fingerprint over the JSON files alone would have missed every change that
actually moved this project's reliability figure from 65% to 85%.

Run:
    python tests/unit/test_config_version.py
"""

import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm import version  # noqa: E402
from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import load_events  # noqa: E402

CONFIG, TRANSITIONS = load_config()
EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))


def fp(config=None, transitions=None):
    return version.config_fingerprint(
        CONFIG if config is None else config,
        TRANSITIONS if transitions is None else transitions,
    )


# --- shape -------------------------------------------------------------------

def test_fingerprint_is_short_stable_hex():
    value = fp()
    assert len(value) == version.SHORT_LEN
    assert all(c in "0123456789abcdef" for c in value), value
    assert value == fp(), "same inputs must give the same hash"


# --- it changes when it should -----------------------------------------------

def test_changing_a_model_changes_the_fingerprint():
    other = copy.deepcopy(CONFIG)
    other["models"]["checker"] = "some-other-model:1b"
    assert fp(other) != fp()


def test_changing_the_rules_table_changes_the_fingerprint():
    other = copy.deepcopy(TRANSITIONS)
    other["by_mood"]["alert"]["disallowed_next_action"] = ["celebrate"]
    assert fp(transitions=other) != fp()


def test_changing_the_memory_window_changes_the_fingerprint():
    other = copy.deepcopy(CONFIG)
    other["recent_events_max_age_s"] = 30
    assert fp(other) != fp()


def test_changing_a_prompt_changes_the_fingerprint():
    """The whole point: prompts are code, and they are what moves the numbers."""
    before = fp()
    original = version.PROMPTS["mood"]
    try:
        version.PROMPTS["mood"] = original + "\nAnd one more clause."
        assert fp() != before
    finally:
        version.PROMPTS["mood"] = original
    assert fp() == before, "restoring the prompt must restore the hash"


def test_real_vs_mocked_do_not_share_a_fingerprint():
    """A mocked run and a real run are not the same measurement."""
    mocked = copy.deepcopy(CONFIG)
    mocked["real_personas"] = []
    real = copy.deepcopy(CONFIG)
    real["real_personas"] = ["mood", "action", "line", "checker"]
    assert fp(mocked) != fp(real)


# --- it does NOT change when it shouldn't ------------------------------------

def test_editing_a_documentation_key_does_not_change_the_fingerprint():
    """`_`-prefixed keys are commentary; rewording one is not a new measurement."""
    other = copy.deepcopy(CONFIG)
    other["_comment"] = "totally rewritten note that changes no behaviour"
    assert fp(other) == fp()


def test_key_order_does_not_change_the_fingerprint():
    other = {k: CONFIG[k] for k in reversed(list(CONFIG))}
    assert fp(other) == fp()


# --- it reaches the trace ----------------------------------------------------

def test_every_trace_record_carries_the_fingerprint():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        with Harness(CONFIG, TRANSITIONS, trace_path=path) as h:
            h.run(EVENTS, idle_ticks=2)
        records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    assert records
    expected = fp()
    # Per-record, not per-file: traces get concatenated and sliced by hand, and a
    # header-only stamp would survive that as a lie.
    for record in records:
        assert record["config_version"] == expected, record["tick"]


def test_sidecar_explains_what_the_hash_expands_to():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        with Harness(CONFIG, TRANSITIONS, trace_path=path,
                     scenario="demo_sequence.json", mode="mocked") as h:
            h.run(EVENTS, idle_ticks=1)
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))

    assert meta["config_version"] == fp()
    assert meta["scenario"] == "demo_sequence.json"
    assert meta["mode"] == "mocked"
    assert set(meta["prompt_hashes"]) == {"mood", "action", "line", "check"}
    assert meta["models"] == dict(sorted(CONFIG["models"].items()))
    # The sidecar must be enough to reconstruct the hash without the repo at
    # that commit, so it carries the configs themselves, doc keys stripped.
    assert "_comment" not in meta["personas_config"]
    assert meta["transitions_config"]["by_mood"] == TRANSITIONS["by_mood"]


def test_no_sidecar_and_no_crash_when_tracing_is_off():
    h = Harness(CONFIG, TRANSITIONS, trace_path=None)
    with h:
        records = h.run(EVENTS, idle_ticks=1)
    assert records and all(r["config_version"] == fp() for r in records)


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
