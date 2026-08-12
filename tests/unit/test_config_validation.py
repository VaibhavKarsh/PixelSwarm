"""Phase 1 negative + edge tests for scripts/validate_configs.py.

docs/06_TESTING_STRATEGY.md Phase 1 requires more than "the validator passes on good
input" - it requires proof that the validator FAILS, loudly and specifically, on
each way a config can be wrong. A validator that silently accepts a broken
config is worse than none, because it converts a loud failure into a quiet one
three phases later.

Each test writes mutated configs to a temp directory, points the validator's
path constants at them, and asserts both the exit code and that the message
names the offending field. The real config/ files are never touched.

Runs standalone with no test framework (Section 9 forbids new dependencies):
    python tests/unit/test_config_validation.py
It is also plain pytest-compatible if pytest is ever added.
"""

import copy
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_configs  # noqa: E402

# The real, known-good configs are the baseline every test mutates.
GOOD_PERSONAS = json.loads((REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8"))
GOOD_TRANSITIONS = json.loads((REPO_ROOT / "config" / "transitions.json").read_text(encoding="utf-8"))
GOOD_SEQUENCE = json.loads((REPO_ROOT / "events" / "demo_sequence.json").read_text(encoding="utf-8"))


def run(personas=None, transitions=None, sequence=None):
    """Run the validator against the given configs. Values may be Python
    objects (serialised to JSON) or raw strings (written verbatim, so a test can
    produce things json.dumps cannot, like duplicate keys).

    Returns (exit_code, combined_output).
    """
    payloads = {
        "personas": GOOD_PERSONAS if personas is None else personas,
        "transitions": GOOD_TRANSITIONS if transitions is None else transitions,
        "sequence": GOOD_SEQUENCE if sequence is None else sequence,
    }

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        written = {}
        for name, value in payloads.items():
            target = tmp_path / f"{name}.json"
            text = value if isinstance(value, str) else json.dumps(value, indent=2)
            target.write_text(text, encoding="utf-8")
            written[name] = target

        original = (validate_configs.PERSONAS, validate_configs.TRANSITIONS, validate_configs.SEQUENCE)
        validate_configs.PERSONAS = written["personas"]
        validate_configs.TRANSITIONS = written["transitions"]
        validate_configs.SEQUENCE = written["sequence"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = validate_configs.main()
        finally:
            validate_configs.PERSONAS, validate_configs.TRANSITIONS, validate_configs.SEQUENCE = original

    return code, out.getvalue() + err.getvalue()


def expect_fail(fragment, **kwargs):
    """Assert the validator rejects this input AND explains why."""
    code, output = run(**kwargs)
    assert code == 1, f"expected failure but got exit {code}.\nOutput:\n{output}"
    assert fragment.lower() in output.lower(), (
        f"failed as expected, but the message never mentioned {fragment!r}.\n"
        f"A vague failure is nearly as bad as none.\nOutput:\n{output}"
    )


def expect_pass(**kwargs):
    code, output = run(**kwargs)
    assert code == 0, f"expected success but got exit {code}.\nOutput:\n{output}"
    return output


# --- baseline ----------------------------------------------------------------

def test_real_configs_pass():
    expect_pass()


def test_canonical_sequence_matches_demo_script_table():
    """Phase 1's Definition of Done: demo_sequence.json must reproduce the Demo
    Script Section 1 table exactly.

    This runs the validator against the REAL repo paths (not temp copies), which
    is the only configuration in which the table-conformance check is active.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = validate_configs.main()
    output = out.getvalue() + err.getvalue()
    assert code == 0, f"the real repo configs do not validate.\nOutput:\n{output}"
    assert "checked against Demo Script Section 1 table" in output, (
        "the table-conformance check did not run against the canonical sequence file, "
        f"so the Phase 1 DoD is unverified.\nOutput:\n{output}"
    )


# --- personas.json: negative -------------------------------------------------

def test_personas_missing_models():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    del cfg["models"]
    expect_fail("models", personas=cfg)


def test_personas_missing_one_persona():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    del cfg["models"]["checker"]
    expect_fail("checker", personas=cfg)


def test_personas_unknown_persona():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["models"]["narrator"] = "qwen3.5:9b"
    expect_fail("narrator", personas=cfg)


def test_personas_empty_model_name():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["models"]["mood"] = "   "
    expect_fail("models.mood", personas=cfg)


def test_personas_mood_enum_drifts_from_doc():
    # The exact bug this catches: config says one thing, the prompt says another,
    # so the model is told an enum the harness will not accept.
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["moods"] = cfg["moods"] + ["neutral"]
    expect_fail("does not match", personas=cfg)


def test_personas_action_enum_drifts_from_doc():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["actions"] = ["idle_loop", "wave", "jump", "duck", "celebrate", "spin"]
    expect_fail("does not match", personas=cfg)


def test_personas_duplicate_json_key():
    # json.loads keeps the last value silently; the validator must not.
    raw = '{"moods": ["idle"], "moods": ["happy"]}'
    expect_fail("duplicate key", personas=raw)


def test_personas_think_true_is_rejected():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["runtime"]["think"] = True
    expect_fail("think", personas=cfg)


def test_personas_missing_timeout():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    del cfg["runtime"]["timeout_s"]
    expect_fail("timeout_s", personas=cfg)


def test_personas_zero_timeout():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["runtime"]["timeout_s"] = 0
    expect_fail("timeout_s", personas=cfg)


def test_personas_fallback_action_not_in_enum():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["fallback"]["action"] = "moonwalk"
    expect_fail("fallback.action", personas=cfg)


def test_personas_window_wrong_type():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["recent_events_max_count"] = "five"
    expect_fail("recent_events_max_count", personas=cfg)


def test_personas_tick_timer_zero():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["tick_timer_s"] = 0
    expect_fail("tick_timer_s", personas=cfg)


def test_personas_bool_is_not_a_number():
    # bool is a subclass of int in Python; the validator must not accept it.
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["tick_timer_s"] = True
    expect_fail("tick_timer_s", personas=cfg)


def test_personas_malformed_json():
    expect_fail("invalid json", personas='{"moods": [')


def test_personas_empty_file():
    expect_fail("empty", personas="")


# --- transitions.json: negative ----------------------------------------------

def test_transitions_unknown_mood_key():
    expect_fail("hangry", transitions={"by_mood": {"hangry": {"disallowed_next_action": ["wave"]}}})


def test_transitions_unknown_action():
    expect_fail("moonwalk", transitions={"by_mood": {"sad": {"disallowed_next_action": ["moonwalk"]}}})


def test_transitions_unknown_field():
    expect_fail(
        "unknown field",
        transitions={"by_mood": {"sad": {"disallowed_next_action": ["jump"], "allowed_next_mood": ["idle"]}}},
    )


def test_transitions_disallowing_every_action_is_unreachable():
    every = list(GOOD_PERSONAS["actions"])
    expect_fail("every action", transitions={"by_mood": {"sad": {"disallowed_next_action": every}}})


def test_transitions_wrong_shape():
    expect_fail("must be a list", transitions={"by_mood": {"sad": {"disallowed_next_action": "jump"}}})


# --- transitions.json: the two-section structure (Architecture doc Section 4) --

def test_transitions_flat_legacy_format_is_rejected_loudly():
    """The pre-2026-07-30 flat format must fail, not be silently ignored.

    Silently ignoring it would be the worst outcome: the rules would look present
    in the file while enforcing nothing, which is exactly how the t=60 conflict
    went missing in the first place.
    """
    expect_fail("no longer read", transitions={"sad": {"disallowed_next_action": ["celebrate"]}})


def test_transitions_unknown_previous_action_key():
    expect_fail(
        "moonwalk",
        transitions={"by_previous_action": {"moonwalk": {"disallowed_next_action": ["wave"]}}},
    )


def test_transitions_by_previous_action_unknown_banned_action():
    expect_fail(
        "boogie",
        transitions={"by_previous_action": {"celebrate": {"disallowed_next_action": ["boogie"]}}},
    )


def test_transitions_rejects_banning_the_fallback_action_by_mood():
    """Section 5 pins idle_loop as the always-legal commit."""
    expect_fail(
        "fallback action",
        transitions={"by_mood": {"sad": {"disallowed_next_action": ["idle_loop"]}}},
    )


def test_transitions_rejects_banning_the_fallback_action_by_previous_action():
    expect_fail(
        "fallback action",
        transitions={"by_previous_action": {"celebrate": {"disallowed_next_action": ["idle_loop"]}}},
    )


def test_transitions_combined_rules_leaving_nothing_legal_is_rejected():
    """Each section alone is survivable but together they forbid everything."""
    actions = list(GOOD_PERSONAS["actions"])
    half, rest = actions[:3], actions[3:]
    expect_fail(
        "every action",
        transitions={
            "by_mood": {"sad": {"disallowed_next_action": half}},
            "by_previous_action": {"celebrate": {"disallowed_next_action": rest}},
        },
    )


def test_transitions_accepts_the_committed_two_section_table():
    expect_pass(transitions=json.loads(
        (REPO_ROOT / "config" / "transitions.json").read_text(encoding="utf-8-sig")
    ))


# --- demo_sequence.json: negative --------------------------------------------

def test_sequence_unknown_event_type():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["type"] = "chat_lull"
    expect_fail("chat_lull", sequence=seq)


def test_sequence_intensity_above_range():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["intensity"] = 1.5
    expect_fail("intensity", sequence=seq)


def test_sequence_intensity_negative():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["intensity"] = -0.1
    expect_fail("intensity", sequence=seq)


def test_sequence_intensity_wrong_type():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["intensity"] = "high"
    expect_fail("intensity", sequence=seq)


def test_sequence_missing_ts():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    del seq[2]["ts"]
    expect_fail("ts", sequence=seq)


def test_sequence_missing_type():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    del seq[1]["type"]
    expect_fail("type", sequence=seq)


def test_sequence_out_of_order_ts():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[3]["ts"], seq[4]["ts"] = seq[4]["ts"], seq[3]["ts"]
    expect_fail("earlier than", sequence=seq)


def test_sequence_negative_ts():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["ts"] = -5
    expect_fail("ts", sequence=seq)


def test_sequence_not_an_array():
    expect_fail("array", sequence={"events": []})


def test_sequence_element_not_an_object():
    expect_fail("object", sequence=["chat_calm"])


def test_sequence_meta_wrong_type():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["meta"] = "scripted_demo"
    expect_fail("meta", sequence=seq)


# --- edge cases that must be ACCEPTED ----------------------------------------

def test_empty_sequence_is_valid():
    # The harness should run zero ticks cleanly rather than crash - 06 Phase 1.
    output = expect_pass(sequence=[])
    assert "zero ticks" in output, "an empty sequence should be flagged as a NOTE"


def test_single_event_sequence_is_valid():
    expect_pass(sequence=[GOOD_SEQUENCE[0]])


def test_duplicate_ts_is_allowed_but_noted():
    # Interface Contract Section 2.1: ties are allowed, file order breaks them.
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[1]["ts"] = seq[0]["ts"]
    output = expect_pass(sequence=seq)
    assert "sharing a ts" in output, "duplicate ts should be surfaced as a NOTE"


def test_integer_ts_and_intensity_accepted():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    seq[0]["ts"] = 15
    seq[0]["intensity"] = 1
    expect_pass(sequence=seq)


def test_meta_may_be_absent():
    seq = copy.deepcopy(GOOD_SEQUENCE)
    del seq[0]["meta"]
    expect_pass(sequence=seq)


def test_underscore_keys_are_ignored():
    cfg = copy.deepcopy(GOOD_PERSONAS)
    cfg["_scratch"] = {"anything": [1, 2, 3]}
    expect_pass(personas=cfg)


# --- runner ------------------------------------------------------------------

def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"  FAIL  {name}")
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR {name}")

    print("-" * 68)
    if failures:
        print(f"FAIL: {len(failures)} of {len(tests)} test(s) failed:", file=sys.stderr)
        for name, exc in failures:
            print(f"\n--- {name} ---\n{exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(tests)} config-validation tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
