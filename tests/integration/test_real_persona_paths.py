"""Phase 3 integration tests for the REAL persona code paths, driven by the fake
model client (docs/06_TESTING_STRATEGY.md Sections 2.1 and Phase 3).

These exercise `_real_*` in swarm/personas.py - prompt assembly, the model call,
JSON extraction, the harness's enum checks and the Section 5 fallbacks - without
a live Ollama. That matters because the interesting cases (a stall, a truncated
response, an invented enum value) are rare and unschedulable against a real
model, but must be routine in a test suite.

The live-model checks live in test_personas_live.py and are skipped when the
server is absent; these run always.

    python tests/integration/test_real_persona_paths.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))

from swarm import personas  # noqa: E402
from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import banned_actions, load_events  # noqa: E402

from fake_model_client import (  # noqa: E402
    MALFORMED,
    VALID,
    FakeModelClient,
    persona_of,
)

BASE_CONFIG, TRANSITIONS = load_config()
DEMO_EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))
ALL_PERSONAS = ["mood", "action", "line", "checker"]


def config_with(client, real=ALL_PERSONAS, grounded_guard=True):
    cfg = dict(BASE_CONFIG)
    cfg["real_personas"] = list(real)
    cfg["_client"] = client
    # Section 5's grounded-rejection guard sits in FRONT of the illegal-commit
    # invariant and handles one scenario they share (checker rejects a legal
    # action and names an illegal fallback). Tests aimed at the invariant turn
    # it off so they reach the mechanism they are named after.
    cfg["enforce_grounded_rejections"] = grounded_guard
    return cfg


def run(client, real=ALL_PERSONAS, events=None, idle_ticks=0, grounded_guard=True):
    cfg = config_with(client, real, grounded_guard)
    with Harness(cfg, TRANSITIONS) as h:
        return h, h.run(events if events is not None else DEMO_EVENTS, idle_ticks=idle_ticks)


# --- the fixture identifies personas by their Section 3.5 prompts ------------

def test_persona_of_recognises_every_real_prompt():
    assert persona_of(personas.MOOD_PROMPT) == "mood"
    assert persona_of(personas.ACTION_PROMPT) == "action"
    assert persona_of(personas.LINE_PROMPT) == "line"
    assert persona_of(personas.checker_prompt(TRANSITIONS)) == "check"


# --- dispatch: real vs mock, per persona ------------------------------------

def test_default_config_uses_no_real_personas():
    assert not BASE_CONFIG.get("real_personas"), "the committed config must default to mocked"


def test_only_the_selected_persona_calls_the_model():
    """The Roadmap's core Phase 3 workflow: mood real, the rest still mocked."""
    client = FakeModelClient()
    run(client, real=["mood"])
    assert client.call_count("mood") == len(DEMO_EVENTS)
    for other in ("action", "line", "check"):
        assert client.call_count(other) == 0, f"{other} should still be mocked"


def test_enabling_personas_incrementally_adds_exactly_one_caller():
    for count, selection in enumerate(
        (["mood"], ["mood", "action"], ["mood", "action", "line"], ALL_PERSONAS), start=1
    ):
        client = FakeModelClient()
        run(client, real=selection)
        callers = {p for p, _ in client.calls}
        assert len(callers) == count, f"{selection} -> {callers}"


def test_all_four_real_personas_call_the_model_once_per_tick():
    client = FakeModelClient()
    _, records = run(client)
    assert client.call_count() == len(records) * 4


def test_each_persona_is_sent_its_configured_model():
    client = FakeModelClient()
    run(client, real=["mood"])
    assert {m for _, m in client.calls} == {BASE_CONFIG["models"]["mood"]}


# --- a clean real-path run ---------------------------------------------------

def test_clean_run_produces_no_errors_and_valid_state():
    client = FakeModelClient()
    _, records = run(client, idle_ticks=2)
    for r in records:
        assert not any(r["errors"].values()), (r["tick"], r["errors"])
        assert r["final_state"]["current_mood"] in BASE_CONFIG["moods"]
        assert r["final_state"]["current_action"] in BASE_CONFIG["actions"]


def test_queued_responses_drive_the_committed_state():
    client = FakeModelClient()
    client.queue("mood", VALID["mood"]["excited"])
    client.queue("action", VALID["action"]["celebrate"])
    client.queue("check", VALID["check"]["approve"])
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert records[0]["final_state"]["current_mood"] == "excited"
    assert records[0]["final_state"]["current_action"] == "celebrate"


# --- failure taxonomy: 06 requires these be DISTINGUISHABLE ------------------

def test_timeout_is_reported_as_timeout():
    client = FakeModelClient()
    client.queue_timeout("mood")
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert records[0]["errors"]["mood"].startswith("timeout:")


def test_unavailable_is_reported_as_unavailable():
    client = FakeModelClient()
    client.queue_unavailable("mood")
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert records[0]["errors"]["mood"].startswith("unavailable:")


def test_parse_failure_is_reported_as_parse_failure():
    client = FakeModelClient()
    client.queue("mood", MALFORMED["prose_only"])
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert records[0]["errors"]["mood"].startswith("parse_failure:")


def test_out_of_enum_is_reported_distinctly_from_parse_failure():
    client = FakeModelClient()
    client.queue("mood", MALFORMED["out_of_enum"])
    _, records = run(client, events=DEMO_EVENTS[:1])
    reason = records[0]["errors"]["mood"]
    assert "out_of_enum" in reason
    assert not reason.startswith("parse_failure:"), (
        "a well-formed response with a bad value must not be conflated with unparseable output"
    )


def test_the_four_failure_classes_are_mutually_distinguishable():
    """06 Phase 3: 'don't collapse both into one generic error'."""
    seen = {}
    for label, setup in (
        ("timeout", lambda c: c.queue_timeout("mood")),
        ("unavailable", lambda c: c.queue_unavailable("mood")),
        ("parse_failure", lambda c: c.queue("mood", MALFORMED["truncated"])),
        ("out_of_enum", lambda c: c.queue("mood", MALFORMED["out_of_enum"])),
    ):
        client = FakeModelClient()
        setup(client)
        _, records = run(client, events=DEMO_EVENTS[:1])
        seen[label] = records[0]["errors"]["mood"]
    assert len(set(seen.values())) == 4, seen
    for label, reason in seen.items():
        assert label in reason, f"{label} not identifiable in {reason!r}"


# --- fallbacks on the real path (Section 5) ---------------------------------

def test_a_failed_real_persona_falls_back_and_the_run_continues():
    client = FakeModelClient()
    for _ in DEMO_EVENTS:
        client.queue("mood", MALFORMED["empty"])
    _, records = run(client)
    assert len(records) == len(DEMO_EVENTS)
    for r in records:
        assert r["errors"]["mood"]
        assert r["proposals"]["mood"]["mood"] == r["input_state"]["current_mood"]


def test_a_fallback_proposal_still_passes_through_the_checker():
    """Section 5, clarified 2026-07-30: a substituted fallback is still only a
    proposal - it must not bypass step 5."""
    client = FakeModelClient()
    client.queue("action", MALFORMED["empty"])
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert records[0]["errors"]["action"]
    assert records[0]["verdict"]["verdict"] in ("approve", "reject")
    assert records[0]["errors"]["check"] is None, "the checker itself did not fail"


def test_checker_echoing_the_forbidden_action_is_caught_by_the_invariant():
    """The exact pre-fix failure Phase 0 measured on every real model."""
    client = FakeModelClient()
    client.queue("mood", VALID["mood"]["alert"])
    client.queue("action", VALID["action"]["celebrate"])
    client.queue("check", MALFORMED["echoes_forbidden"])
    _, records = run(client, events=DEMO_EVENTS[:1])
    r = records[0]
    assert r["final_state"]["current_action"] != "celebrate"
    assert "harness invariant" in (r["errors"]["check"] or "")


def test_checker_returning_an_illegal_in_enum_action_is_caught():
    client = FakeModelClient()
    client.queue("check", MALFORMED["illegal_fallback"])   # reject/alert/jump
    _, records = run(client, events=DEMO_EVENTS[:1], grounded_guard=False)
    r = records[0]
    assert r["final_state"]["current_action"] not in TRANSITIONS["by_mood"]["alert"]["disallowed_next_action"]
    assert "harness invariant" in (r["errors"]["check"] or "")


def test_illegal_fallback_is_still_blocked_with_the_grounded_guard_on():
    """The shipped configuration must reach the same outcome by either route."""
    client = FakeModelClient()
    client.queue("check", MALFORMED["illegal_fallback"])   # reject/alert/jump
    _, records = run(client, events=DEMO_EVENTS[:1])
    r = records[0]
    assert r["final_state"]["current_action"] not in TRANSITIONS["by_mood"]["alert"]["disallowed_next_action"]
    assert ("overruled" in r["verdict"]) or ("harness invariant" in (r["errors"]["check"] or ""))


def test_checker_inventing_an_action_is_a_persona_failure():
    client = FakeModelClient()
    client.queue("check", MALFORMED["invented_action"])   # defensive_stance
    _, records = run(client, events=DEMO_EVENTS[:1])
    assert "out_of_enum" in records[0]["errors"]["check"]
    assert records[0]["verdict"]["verdict"] == "harness_fallback"


def test_every_model_failure_still_yields_a_committable_state():
    """Whatever the model does, the harness must always commit something legal."""
    for name in MALFORMED:
        client = FakeModelClient()
        for persona in ("mood", "action", "line", "check"):
            client.queue(persona, MALFORMED[name])
        _, records = run(client, events=DEMO_EVENTS[:1])
        r = records[0]
        mood, action = r["final_state"]["current_mood"], r["final_state"]["current_action"]
        assert mood in BASE_CONFIG["moods"], (name, mood)
        assert action in BASE_CONFIG["actions"], (name, action)
        # Use the shared helper, not a hand-rolled lookup: indexing TRANSITIONS
        # by mood directly returns {} under the two-section schema, which made
        # this assertion silently vacuous until 2026-07-30.
        banned = banned_actions(TRANSITIONS, mood, r["input_state"]["current_action"])
        assert action not in banned, (name, mood, action)


def test_a_stall_on_one_persona_does_not_stop_the_run():
    client = FakeModelClient()
    client.queue_timeout("check")
    _, records = run(client, idle_ticks=1)
    assert len(records) == len(DEMO_EVENTS) + 1
    assert records[0]["errors"]["check"].startswith("timeout:")
    assert all(r["errors"]["check"] is None for r in records[1:])


# --- prompt integrity on the wire -------------------------------------------

def test_the_checker_prompt_carries_the_live_transitions_table():
    prompt = personas.checker_prompt(TRANSITIONS)
    assert "disallowed_next_action" in prompt
    for section in ("by_mood", "by_previous_action"):
        assert section in prompt, section
        for key in TRANSITIONS[section]:
            assert key in prompt, f"{section}.{key} missing from the prompt"


def test_the_checker_prompt_excludes_documentation_keys():
    """`_`-prefixed keys are maintainer commentary and must not reach the model.

    One of them discusses the action-picker, which is exactly the sort of stray
    text that misleads a small model reading a rules table - and it also broke
    persona identification in the test fixture until it was stripped.
    """
    prompt = personas.checker_prompt(TRANSITIONS)
    doc_keys = [k for k in TRANSITIONS if k.startswith("_")]
    assert doc_keys, "fixture assumption: the committed table has documentation keys"
    for key in doc_keys:
        assert key not in prompt, f"{key} leaked into the model prompt"
    assert "action-picker" not in prompt.lower()


def test_prompts_are_sent_verbatim_not_reformatted():
    captured = {}

    class Recorder(FakeModelClient):
        def chat(self, model, system_prompt, user_payload, runtime=None, client=None):
            captured[persona_of(system_prompt)] = system_prompt
            return super().chat(model, system_prompt, user_payload, runtime, client)

    run(Recorder(), events=DEMO_EVENTS[:1])
    assert captured["mood"] == personas.MOOD_PROMPT
    assert captured["action"] == personas.ACTION_PROMPT
    assert captured["line"] == personas.LINE_PROMPT


def test_each_persona_receives_the_inputs_its_contract_specifies():
    captured = {}

    class Recorder(FakeModelClient):
        def chat(self, model, system_prompt, user_payload, runtime=None, client=None):
            captured[persona_of(system_prompt)] = user_payload
            return super().chat(model, system_prompt, user_payload, runtime, client)

    run(Recorder(), events=DEMO_EVENTS[:1])
    # Sections 3.1-3.4: each persona sees the state plus exactly the prior
    # proposals, and no more.
    assert set(captured["mood"]) == {"state"}
    assert set(captured["action"]) == {"state", "proposed_mood"}
    assert set(captured["line"]) == {"state", "proposed_mood", "proposed_action"}
    assert set(captured["check"]) == {"state", "proposed_mood", "proposed_action", "proposed_line"}


def test_client_is_cached_per_host_and_timeout():
    """A run must reuse one connection pool, not build a client per call.

    Constructing an ollama.Client opens no connection, so this is safe offline.
    """
    from swarm import model_client

    model_client.reset_client_cache()
    runtime = {"host": "http://127.0.0.1:11434", "timeout_s": 30}
    first = model_client.get_client(runtime)
    assert model_client.get_client(dict(runtime)) is first, "same settings must reuse the client"

    other = model_client.get_client({"host": "http://127.0.0.1:11434", "timeout_s": 60})
    assert other is not first, "a different timeout must not silently reuse the old client"

    model_client.reset_client_cache()
    assert model_client.get_client(runtime) is not first, "reset must drop cached clients"


def test_runtime_settings_reach_the_client():
    captured = {}

    class Recorder(FakeModelClient):
        def chat(self, model, system_prompt, user_payload, runtime=None, client=None):
            captured.update(runtime or {})
            return super().chat(model, system_prompt, user_payload, runtime, client)

    run(Recorder(), events=DEMO_EVENTS[:1], real=["mood"])
    assert captured.get("think") is False, "think=False must reach the client (Section 7.1)"
    assert captured.get("format") == "json"
    assert captured.get("timeout_s")


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
    print(f"OK: {len(tests)} real-path tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
