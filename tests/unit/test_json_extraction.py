"""Phase 3 unit tests for the JSON extractor (Architecture doc Section 3.6).

docs/06_TESTING_STRATEGY.md Phase 3: "this is where most of the rigor for this phase
belongs, and it doesn't need a real model at all." Every malformed shape in the
fixture battery is exercised here, deterministically, in milliseconds.

The distinction these tests protect: `format="json"` guarantees at best
PARSEABLE JSON, never correct keys or in-enum values (Section 7.2). So the
extractor's only job is text -> dict, and it must be honest about failing rather
than returning something the harness would mistake for a real proposal.

    python tests/unit/test_json_extraction.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))

from swarm.model_client import ModelParseFailure, extract_json  # noqa: E402

from fake_model_client import MALFORMED, PARSEABLE, VALID  # noqa: E402


def raises_parse_failure(text):
    try:
        extract_json(text)
    except ModelParseFailure:
        return True
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected ModelParseFailure, got {type(exc).__name__}: {exc}") from exc
    return False


# --- the happy path ----------------------------------------------------------

def test_plain_json_object_parses():
    assert extract_json('{"mood": "idle", "confidence": 0.5, "reason": "r"}')["mood"] == "idle"


def test_every_canned_valid_response_parses():
    for persona, cases in VALID.items():
        for name, raw in cases.items():
            parsed = extract_json(raw)
            assert isinstance(parsed, dict), f"{persona}/{name}"


def test_whitespace_around_json_is_tolerated():
    assert extract_json('\n\n  {"mood": "idle", "reason": "r"}  \n')["mood"] == "idle"


def test_nested_objects_survive_extraction():
    # The greedy first-{ to last-} scan must not truncate at an inner brace.
    parsed = extract_json('prefix {"a": {"b": {"c": 1}}, "d": 2} suffix')
    assert parsed["a"]["b"]["c"] == 1 and parsed["d"] == 2


# --- the malformed battery (06 Section 2.1) ----------------------------------

def test_markdown_fenced_json_is_recovered():
    # A very common small-model quirk; losing a tick to it would be wasteful.
    assert extract_json(MALFORMED["fenced"])["mood"] == "happy"


def test_prose_wrapped_json_is_recovered():
    assert extract_json(MALFORMED["prose_wrapped"])["mood"] == "sad"


def test_empty_response_raises():
    assert raises_parse_failure(MALFORMED["empty"])


def test_whitespace_only_response_raises():
    assert raises_parse_failure(MALFORMED["whitespace"])


def test_prose_with_no_json_raises():
    assert raises_parse_failure(MALFORMED["prose_only"])


def test_truncated_json_raises():
    assert raises_parse_failure(MALFORMED["truncated"])


def test_array_is_rejected_as_not_an_object():
    assert raises_parse_failure(MALFORMED["array_not_object"])


def test_bare_scalar_is_rejected():
    assert raises_parse_failure(MALFORMED["scalar_not_object"])


def test_json_null_is_rejected():
    assert raises_parse_failure(MALFORMED["null"])


def test_none_input_raises():
    assert raises_parse_failure(None)


def test_the_whole_battery_matches_its_documented_parseability():
    """Every fixture is classified in PARSEABLE; drift there would silently
    weaken the other tests, so assert the classification itself."""
    for name, raw in MALFORMED.items():
        expected = PARSEABLE[name]
        actually_parsed = not raises_parse_failure(raw)
        assert actually_parsed == expected, (
            f"{name}: PARSEABLE says {expected}, extractor says {actually_parsed}"
        )


# --- the crucial distinction: parseable is NOT valid -------------------------

def test_out_of_enum_value_parses_but_is_not_the_extractors_problem():
    # Section 7.2: JSON mode cannot enforce enums. The extractor must pass this
    # through so the harness's enum check is what rejects it - collapsing the two
    # would lose the distinction between "unparseable" and "wrong value".
    assert extract_json(MALFORMED["out_of_enum"])["mood"] == "neutral"


def test_missing_key_parses_cleanly():
    assert "mood" not in extract_json(MALFORMED["missing_key"])


def test_wrong_typed_value_parses_cleanly():
    assert extract_json(MALFORMED["wrong_type"])["mood"] == 42


def test_illegal_but_in_enum_fallback_parses():
    # reject/alert/jump: well-formed, in-enum, still forbidden. Catching this is
    # the harness invariant's job, not the extractor's.
    assert extract_json(MALFORMED["illegal_fallback"])["final_action"] == "jump"


# --- failure messages are useful --------------------------------------------

# --- the over-escaping repair (Phase 6, measured) ----------------------------

def test_over_escaped_quotes_are_repaired():
    """The measured qwen3.5:9b quirk: 10 of 16 Phase 4 baseline failures."""
    parsed = extract_json(MALFORMED["over_escaped"])
    assert parsed["line"] is None
    assert parsed["reason"] == "no line needed yet."


def test_over_escaped_values_are_repaired():
    parsed = extract_json(MALFORMED["over_escaped_mood"])
    assert parsed["mood"] == "idle" and parsed["reason"] == "quiet"


def test_over_escaping_inside_prose_is_still_repaired():
    raw = 'Here you go: {"line": null, "reason": \\"done\\"} hope that helps'
    assert extract_json(raw)["reason"] == "done"


def test_legitimately_escaped_quotes_are_untouched():
    """The repair must never corrupt valid JSON.

    Valid input parses on the first attempt and never reaches the repair pass,
    so an embedded quote survives intact.
    """
    raw = '{"line": "he said \\"hi\\"", "reason": "quoting"}'
    assert extract_json(raw)["line"] == 'he said "hi"'


def test_repair_does_not_rescue_genuinely_broken_input():
    # The repair is not a licence to accept anything: truncated stays truncated.
    assert raises_parse_failure('{"line": null, "reason": \\"unterminated')
    assert raises_parse_failure(MALFORMED["prose_only"])


def test_parse_failure_messages_distinguish_the_cause():
    causes = {}
    for name in ("empty", "prose_only", "truncated", "array_not_object"):
        try:
            extract_json(MALFORMED[name])
        except ModelParseFailure as exc:
            causes[name] = str(exc)
    assert "empty" in causes["empty"].lower()
    assert "no json object" in causes["prose_only"].lower()
    assert "malformed" in causes["truncated"].lower()
    assert "expected object" in causes["array_not_object"].lower()


def test_parse_failure_message_is_truncated_not_unbounded():
    # A model that returns a wall of prose should not paste all of it into the
    # trace log, which has to stay readable.
    try:
        extract_json("no json here " * 500)
    except ModelParseFailure as exc:
        assert len(str(exc)) < 200


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
    print(f"OK: {len(tests)} JSON extraction tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
