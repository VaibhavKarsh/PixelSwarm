"""Phase 3 LIVE model checks (docs/06_TESTING_STRATEGY.md Phase 3, Definition of Done).

The Roadmap's per-persona bar: "that persona's real model call produces valid
JSON matching its schema on at least 10 consecutive ticks in a test run", plus a
deliberately broken call triggering the documented fallback rather than a crash.

This is the only file in the suite that needs a live Ollama. It SKIPS (exit 0)
when the server or the configured model is unavailable, so `tests/run_all.py`
stays deterministic on a machine without them - a skip is reported loudly rather
than silently passing.

Slow by nature: 4 personas x 10 calls against a 9b model takes minutes. Run it
deliberately, not in a tight edit loop.

    python tests/integration/test_personas_live.py
    python tests/integration/test_personas_live.py --runs 10 --persona mood
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm import personas  # noqa: E402
from swarm.harness import load_config, load_json, validate_proposal  # noqa: E402
from swarm.model_client import ModelError, get_client  # noqa: E402
from swarm.state import SwarmState, load_events  # noqa: E402

CONFIG, TRANSITIONS = load_config()
DEMO_EVENTS = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))
REQUIRED_PASS_RATE = 1.0   # the Roadmap says valid on at least 10 CONSECUTIVE ticks


def server_available():
    """True if the runtime answers and every configured model is pulled."""
    runtime = CONFIG.get("runtime", {})
    try:
        client = get_client(runtime)
        pulled = {m.get("model") or m.get("name") for m in client.list().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach {runtime.get('host')}: {type(exc).__name__}: {exc}"
    missing = {m for m in CONFIG["models"].values() if m not in pulled}
    if missing:
        return False, f"model(s) not pulled: {', '.join(sorted(missing))}"
    return True, ""


def build_state(upto_ts=60.0):
    """State as of the t=60 tick - the most demanding input in the sequence."""
    state = SwarmState(
        max_events=CONFIG["recent_events_max_count"],
        max_age_s=float(CONFIG["recent_events_max_age_s"]),
    )
    state.current_mood, state.current_action = "excited", "celebrate"
    for event in DEMO_EVENTS:
        if event.ts <= upto_ts:
            state.add_event(event, now_ts=upto_ts)
    return state


PROPOSALS = {
    "mood": {"mood": "alert", "confidence": 0.8, "reason": "danger event just fired"},
    "action": {"action": "celebrate", "confidence": 0.7, "reason": "hype momentum"},
    "line": {"line": None, "reason": "no line needed"},
}


def call_once(persona, state):
    """One real call, returning (payload, error_or_None)."""
    cfg = dict(CONFIG)
    cfg["real_personas"] = [persona]
    try:
        if persona == "mood":
            payload = personas.mood_picker(state, config=cfg)
        elif persona == "action":
            payload = personas.action_picker(state, PROPOSALS["mood"], config=cfg)
        elif persona == "line":
            payload = personas.dialogue_line(state, PROPOSALS["mood"], PROPOSALS["action"], config=cfg)
        else:
            payload = personas.transition_checker(state, PROPOSALS, TRANSITIONS, config=cfg)
    except ModelError as exc:
        return None, f"{exc.reason}: {exc}"

    key = {"mood": "mood", "action": "action", "line": "line", "checker": "check"}[persona]
    try:
        validate_proposal(key, payload, CONFIG)
    except Exception as exc:  # noqa: BLE001
        return payload, str(exc)
    return payload, None


def check_persona(persona, runs, state):
    """Roadmap DoD: valid JSON matching the schema on `runs` consecutive calls."""
    print(f"\n  {persona} ({CONFIG['models'][persona]}) x{runs}")
    failures, reasons = 0, []
    for i in range(runs):
        payload, error = call_once(persona, state)
        if error:
            failures += 1
            reasons.append(error)
            print(f"    {i + 1:>2}. FAIL  {error}")
            continue
        # The reason field is what makes the trace explainable (PRD criterion
        # #5), so an empty one counts as a failure even though the schema
        # technically passed. The Roadmap's DoD asks for it explicitly.
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            failures += 1
            reasons.append("empty reason field")
            print(f"    {i + 1:>2}. FAIL  empty reason field")
            continue
        print(f"    {i + 1:>2}. ok    {_summarise(persona, payload)}  | {reason[:52]}")
    passed = runs - failures
    print(f"    -> {passed}/{runs} valid")
    return passed, runs, reasons


def _summarise(persona, payload):
    if persona == "mood":
        return f"mood={payload['mood']}"
    if persona == "action":
        return f"action={payload['action']}"
    if persona == "line":
        return f"line={payload['line']!r}"
    return f"{payload['verdict']}/{payload['final_mood']}/{payload['final_action']}"


def check_fallback_on_a_broken_endpoint():
    """Roadmap DoD: a deliberately broken call must trigger the documented
    fallback, not a crash. Points the client at a dead port."""
    cfg = dict(CONFIG)
    cfg["real_personas"] = ["mood"]
    cfg["runtime"] = dict(CONFIG["runtime"], host="http://127.0.0.1:1", timeout_s=5)
    try:
        personas.mood_picker(build_state(), config=cfg)
    except ModelError as exc:
        print(f"    raised {type(exc).__name__} with reason {exc.reason!r} - harness converts this to a fallback")
        return exc.reason in ("unavailable", "timeout")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL: raised an untyped {type(exc).__name__}: {exc}")
        return False
    print("    FAIL: a dead endpoint returned successfully")
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10, help="consecutive calls per persona")
    parser.add_argument("--persona", default=None, choices=["mood", "action", "line", "checker"])
    args = parser.parse_args(argv)

    ok, why = server_available()
    if not ok:
        print("SKIP: live model checks skipped.")
        print(f"      {why}")
        print("      Start Ollama and pull the models in config/personas.json to run these.")
        return 0

    targets = [args.persona] if args.persona else ["mood", "action", "line", "checker"]
    state = build_state()

    print(f"host  : {CONFIG['runtime']['host']}")
    print(f"runs  : {args.runs} consecutive per persona")
    print("-" * 68)

    results = {}
    for persona in targets:
        results[persona] = check_persona(persona, args.runs, state)

    print("\n  fallback on a dead endpoint")
    fallback_ok = check_fallback_on_a_broken_endpoint()

    print("\n" + "-" * 68)
    problems = []
    for persona, (passed, total, reasons) in results.items():
        rate = passed / total if total else 0
        status = "OK  " if rate >= REQUIRED_PASS_RATE else "FAIL"
        print(f"  {status}  {persona:<8} {passed}/{total} valid")
        if rate < REQUIRED_PASS_RATE:
            problems.append(f"{persona}: {passed}/{total} valid; first reasons: {reasons[:3]}")
    print(f"  {'OK  ' if fallback_ok else 'FAIL'}  broken-endpoint fallback")
    if not fallback_ok:
        problems.append("a broken endpoint did not produce a typed, recoverable failure")

    print("-" * 68)
    if problems:
        print(f"FAIL: {len(problems)} live check(s) failed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("OK: all live persona checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
