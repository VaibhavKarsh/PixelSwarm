"""Phase 0 model benchmark: pick the model(s) that can actually drive the demo.

Architecture doc Section 7 says model choice and tick latency must be resolved
early. This script is the reproducible version of that measurement - the numbers
in Section 7.1 were originally produced ad-hoc, which meant they could not be
re-run when the installed model set changed.

What it measures, per model:
  1. Cold-load and warm per-call latency for all four persona prompts
     (Section 3.5), so a full four-call tick can be costed.
  2. Whether output is schema-valid AND in-enum. `format="json"` guarantees
     parseable JSON, not correct keys or allowed values (Section 7.2).
  3. The two probes that decide whether the demo's headline beat happens at all:
       - mood_shift    : does mood-picker move off `excited` when game_danger
                         arrives at t=60? If it does not, no transition rule has
                         anything to fire on.
       - checker_reject: fed the conflicting combo directly (mood alert +
                         action celebrate), does transition-checker reject and
                         supply a different final_action?
     The original Section 7.1 probe only covered the first of these.

Deliberately standalone, like scripts/smoke_test_model.py: it does not import
from swarm/ and does not read config/, because those are Phase 1+ artifacts
(config/personas.json is still an empty stub). The enums and prompts below are
copied literally from Architecture doc Sections 3.5 and 4 - if those change,
change them here too.

Usage:
    python scripts/benchmark_models.py                    # every installed chat model
    python scripts/benchmark_models.py --models gemma4:e2b,ornith:9b
    python scripts/benchmark_models.py --runs 6 --out results.json
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

try:
    import ollama
except ImportError:
    print("FAIL: the 'ollama' package is not installed.", file=sys.stderr)
    print("Fix: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "http://localhost:11434"
# Generous: a cold 9b load can take a while, and a reasoning model that ignores
# think=False can take minutes. We would rather record a slow number than abort.
DEFAULT_TIMEOUT = 300.0
DEFAULT_RUNS = 4

# --- Copied from Architecture doc Section 3.5 / Section 4. Keep in sync. -------

MOODS = ["idle", "happy", "excited", "alert", "sad", "angry"]
ACTIONS = ["idle_loop", "wave", "jump", "duck", "celebrate", "look_around"]

# Section 4, as amended 2026-07-29 to add the "alert" rule the t=60 beat needs.
TRANSITIONS = {
    "by_mood": {
        "sad": {"disallowed_next_action": ["celebrate", "jump"]},
        "angry": {"disallowed_next_action": ["wave"]},
        "alert": {"disallowed_next_action": ["celebrate", "jump"]},
    },
    "by_previous_action": {
        "celebrate": {"disallowed_next_action": ["duck", "look_around"]},
    },
}

MOOD_PROMPT = """You are the mood-picker for a pixel-art character. Given the character's
current state and recent events, decide the character's new mood.

Allowed moods (choose exactly one): idle, happy, excited, alert, sad, angry

Weigh recent events by urgency, not just by recency or count: threat or danger
signals take precedence over social or hype signals when both are present.

Threat signals are ordered among themselves. An all-clear that arrives after a
danger supersedes it: if the most recent threat-related signal says the danger
has passed, treat the threat as over and let the mood recover, however many
earlier dangers are still in view.

Use idle when nothing is happening: quiet, a timeout, or calm chat with no other
recent event. Calm is an absence of stimulus, not a positive event. But a hype
or positive event IS something happening, so never read one as idle.

Scale positive moods by intensity: a mild positive event is happy, a strong one
(intensity 0.8 or above) is excited. As events recede and nothing new arrives,
settle back toward idle.

An active, unresolved threat is alert. Reserve sad and angry for an actual
negative outcome affecting the character - a loss, a setback, a provocation -
not for the mere presence or absence of a threat. If no recent event describes
such an outcome, do not use them.

Respond with ONLY valid JSON, no other text:
{"mood": "<one of the allowed moods>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}"""

ACTION_PROMPT = """You are the action-picker for a pixel-art character. Given the character's
current state, recent events, and the mood that was just decided, choose
what the character should be doing right now.

Allowed actions (choose exactly one): idle_loop, wave, jump, duck, celebrate, look_around

Typical mood-action pairings (a guide, not a hard rule):
idle -> idle_loop | happy -> wave | excited -> celebrate or jump
alert -> look_around or duck | sad -> idle_loop | angry -> idle_loop

Respond with ONLY valid JSON, no other text:
{"action": "<one of the allowed actions>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}"""

LINE_PROMPT = """You are the dialogue-line picker for a pixel-art character. Given the
character's mood and action just decided, optionally propose a very short
spoken line (under 8 words). Most ticks should have NO line — only propose
one if mood or action just changed meaningfully.

Respond with ONLY valid JSON, no other text:
{"line": "<short line or null>", "reason": "<short reason, max 15 words>"}"""

CHECKER_PROMPT = """You are the transition-checker for a pixel-art character animation system.
You receive the character's current committed state and three proposals
(mood, action, line) from other decision-makers. Check the proposals
against the disallowed-transition rules below. If all proposals are valid,
approve them as-is. If not, reject and supply a valid fallback.

Allowed moods (final_mood must be exactly one of these):
{moods}

Allowed actions (final_action must be exactly one of these):
{actions}

Disallowed transitions:
{transitions}

There are two kinds of rule and BOTH must hold.
by_mood[final_mood] lists actions not allowed while in that mood.
by_previous_action[current_action] lists actions that may not come directly
after the action the character is performing right now - those need an
intermediate step first.

The table is exhaustive: it lists every restriction that exists. A mood with no
by_mood entry restricts nothing, and an action with no by_previous_action entry
may be followed by anything. Approve unless a rule above literally names the
proposed action - never infer a restriction because one seems plausible for the
mood.

If you reject, you MUST pick a final_action that breaks neither rule. Never
return the action you just rejected. Do not fall back to current_action if it
is disallowed. If no better choice is obvious, use idle_loop.

Respond with ONLY valid JSON, no other text:
{{"verdict": "approve" or "reject", "final_mood": "...", "final_action": "...",
 "final_line": "... or null", "reason": "<short reason, max 20 words>"}}""".format(
    moods=", ".join(MOODS),
    actions=", ".join(ACTIONS),
    transitions=json.dumps(TRANSITIONS, indent=2),
)

# --- Fixtures ----------------------------------------------------------------

# A quiet mid-demo tick: used for the plain latency/validity measurements so the
# numbers are not skewed by an unusually dramatic input.
CALM_STATE = {
    "current_mood": "idle",
    "current_action": "idle_loop",
    "last_line": None,
    "ticks_since_last_change": 4,
    "recent_events": [{"type": "chat_calm", "intensity": 0.2, "ts": 15.0}],
}

# The exact t=60 state from Demo Script doc Section 1: hype has escalated to
# excited/celebrate, and game_danger 0.7 has just arrived.
T60_STATE = {
    "current_mood": "excited",
    "current_action": "celebrate",
    "last_line": "Let's go!!",
    "ticks_since_last_change": 1,
    "recent_events": [
        {"type": "chat_hype_spike", "intensity": 0.6, "ts": 30.0},
        {"type": "chat_hype_spike", "intensity": 0.9, "ts": 45.0},
        {"type": "game_danger", "intensity": 0.7, "ts": 60.0},
    ],
}

# The conflicting proposal set the transition-checker must reject: mood has moved
# to alert but the action is still the celebratory one. Section 4's "alert" rule
# disallows exactly this.
T60_CONFLICT_PROPOSALS = {
    "proposed_mood": {"mood": "alert", "confidence": 0.8, "reason": "danger event just fired"},
    "proposed_action": {"action": "celebrate", "confidence": 0.7, "reason": "hype momentum from chat"},
    "proposed_line": {"line": None, "reason": "no line needed"},
}


def extract_json(text):
    """Permissive extractor, per Architecture doc Section 3.6 / 7.2.

    format="json" should make this unnecessary, but Section 7.2 keeps it as a
    second layer. Returns a dict, or None if nothing parseable was found.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _check_confidence(obj):
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return "confidence not a number"
    if not 0.0 <= float(conf) <= 1.0:
        return f"confidence {conf} outside 0.0-1.0"
    return None


def validate(persona, obj):
    """Return None if valid, else a short string naming the first problem."""
    if obj is None:
        return "unparseable"
    if not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
        return "reason missing/empty"

    if persona == "mood":
        if "mood" not in obj:
            return "missing key 'mood'"
        if obj["mood"] not in MOODS:
            return f"out_of_enum:{obj['mood']!r}"
        return _check_confidence(obj)

    if persona == "action":
        if "action" not in obj:
            return "missing key 'action'"
        if obj["action"] not in ACTIONS:
            return f"out_of_enum:{obj['action']!r}"
        return _check_confidence(obj)

    if persona == "line":
        if "line" not in obj:
            return "missing key 'line'"
        if not (obj["line"] is None or isinstance(obj["line"], str)):
            return f"line wrong type: {type(obj['line']).__name__}"
        return None

    if persona == "checker":
        for key in ("verdict", "final_mood", "final_action"):
            if key not in obj:
                return f"missing key {key!r}"
        if obj["verdict"] not in ("approve", "reject"):
            return f"bad verdict:{obj['verdict']!r}"
        if obj["final_mood"] not in MOODS:
            return f"out_of_enum final_mood:{obj['final_mood']!r}"
        if obj["final_action"] not in ACTIONS:
            return f"out_of_enum final_action:{obj['final_action']!r}"
        if "final_line" in obj and not (
            obj["final_line"] is None or isinstance(obj["final_line"], str)
        ):
            return f"final_line wrong type: {type(obj['final_line']).__name__}"
        return None

    raise ValueError(f"unknown persona {persona!r}")


PERSONA_PROMPTS = {
    "mood": MOOD_PROMPT,
    "action": ACTION_PROMPT,
    "line": LINE_PROMPT,
    "checker": CHECKER_PROMPT,
}


def allowed_actions_for(mood, current_action=None):
    """Actions the Section 4 table permits, applying BOTH rule kinds."""
    by_mood = TRANSITIONS["by_mood"].get(mood, {}).get("disallowed_next_action", [])
    by_prev = TRANSITIONS["by_previous_action"].get(current_action, {}).get("disallowed_next_action", [])
    banned = set(by_mood) | set(by_prev)
    return [a for a in ACTIONS if a not in banned]


def build_user_message(persona, state, proposals=None):
    """Assemble the per-persona user payload described in Section 3.1-3.4."""
    payload = {"state": state}
    if persona == "action":
        payload["proposed_mood"] = (proposals or {}).get("proposed_mood")
    elif persona == "line":
        payload["proposed_mood"] = (proposals or {}).get("proposed_mood")
        payload["proposed_action"] = (proposals or {}).get("proposed_action")
    elif persona == "checker":
        payload.update(proposals or {})
    return json.dumps(payload)


class ModelUnavailable(Exception):
    """The model could not be run at all (not pulled, OOM, server refused)."""


def call(client, model, persona, state, proposals=None, allow_think=False):
    """One persona call. Returns (elapsed_s, raw_text, think_unsupported)."""
    messages = [
        {"role": "system", "content": PERSONA_PROMPTS[persona]},
        {"role": "user", "content": build_user_message(persona, state, proposals)},
    ]
    kwargs = {"model": model, "messages": messages, "format": "json"}
    if not allow_think:
        # The Section 7.1 finding: several installed models are reasoning models
        # and are ~100x slower without this.
        kwargs["think"] = False

    started = time.monotonic()
    try:
        response = client.chat(**kwargs)
    except ollama.ResponseError as exc:
        message = (exc.error or "").lower()
        # Some models reject the think parameter outright rather than ignoring
        # it. Retry once without it and record that we had to.
        if "think" in message and not allow_think:
            elapsed, text, _ = call(
                client, model, persona, state, proposals, allow_think=True
            )
            return elapsed, text, True
        if exc.status_code == 404:
            raise ModelUnavailable(f"not pulled (404): {exc.error}") from exc
        raise ModelUnavailable(f"HTTP {exc.status_code}: {exc.error}") from exc
    except (ConnectionError, httpx.ConnectError, ollama.RequestError) as exc:
        # ollama raises the builtin ConnectionError when the server is down.
        raise ModelUnavailable(
            f"cannot reach Ollama at {client._client.base_url}: {type(exc).__name__}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ModelUnavailable(f"timed out: {type(exc).__name__}: {exc}") from exc

    return time.monotonic() - started, (response.message.content or ""), False


def unload(client, model):
    """Free the model's memory before loading the next one.

    Without this, benchmarking several 5-7GB models back to back can push the
    machine into swapping and corrupt the later latency numbers.
    """
    try:
        client.chat(
            model=model,
            messages=[{"role": "user", "content": "bye"}],
            keep_alive=0,
            think=False,
        )
    except Exception:
        pass  # Best-effort only; never fail the benchmark over cleanup.


def benchmark_model(client, model, runs, verbose=True, probes_only=False):
    result = {
        "model": model,
        "available": True,
        "error": None,
        "think_unsupported": False,
        "cold_load_s": None,
        "personas": {},
        "probes": {},
    }

    def log(msg):
        if verbose:
            print(msg, flush=True)

    log(f"\n{'=' * 70}\n{model}\n{'=' * 70}")

    # Cold call: includes model load. Reported separately so it never pollutes
    # the warm averages.
    try:
        cold_s, _, think_unsupported = call(client, model, "mood", CALM_STATE)
    except ModelUnavailable as exc:
        result["available"] = False
        result["error"] = str(exc)
        log(f"  UNAVAILABLE: {exc}")
        return result
    result["cold_load_s"] = round(cold_s, 2)
    result["think_unsupported"] = think_unsupported
    log(f"  cold first call : {cold_s:.2f}s" + ("  (think=False rejected)" if think_unsupported else ""))

    proposals = T60_CONFLICT_PROPOSALS

    for persona in () if probes_only else ("mood", "action", "line", "checker"):
        latencies, failures, samples = [], [], []
        for _ in range(runs):
            try:
                elapsed, text, _ = call(client, model, persona, CALM_STATE, proposals)
            except ModelUnavailable as exc:
                failures.append(str(exc))
                continue
            latencies.append(elapsed)
            obj = extract_json(text)
            problem = validate(persona, obj)
            if problem:
                failures.append(problem)
            samples.append({"raw": text[:400], "parsed": obj, "problem": problem})

        valid = runs - len(failures)
        result["personas"][persona] = {
            "runs": runs,
            "valid": valid,
            "avg_s": round(statistics.mean(latencies), 2) if latencies else None,
            "min_s": round(min(latencies), 2) if latencies else None,
            "max_s": round(max(latencies), 2) if latencies else None,
            "failures": failures,
            "samples": samples,
        }
        avg = f"{statistics.mean(latencies):.2f}s" if latencies else "n/a"
        log(f"  {persona:<8} {valid}/{runs} valid   avg {avg}"
            + (f"   issues: {failures}" if failures else ""))

    per_call = [
        p["avg_s"] for p in result["personas"].values() if p["avg_s"] is not None
    ]
    result["probes_only"] = probes_only
    result["tick_total_s"] = round(sum(per_call), 2) if len(per_call) == 4 else None
    if result["tick_total_s"] is not None:
        log(f"  --> full 4-call tick: {result['tick_total_s']:.2f}s")

    # Probe 1: does mood-picker move off `excited` at t=60?
    moods, mood_raw = [], []
    for _ in range(runs):
        try:
            _, text, _ = call(client, model, "mood", T60_STATE)
        except ModelUnavailable as exc:
            moods.append(f"ERROR:{exc}")
            continue
        obj = extract_json(text)
        mood_raw.append(obj)
        moods.append(obj.get("mood") if isinstance(obj, dict) else "UNPARSEABLE")
    shifted = sum(1 for m in moods if m in MOODS and m != "excited")
    result["probes"]["mood_shift"] = {
        "runs": runs,
        "moods": moods,
        "parsed": mood_raw,  # kept so a failure is diagnosable from the artifact alone
        "shifted_off_excited": shifted,
        "passes": shifted >= (runs // 2 + 1),  # strict majority
    }
    log(f"  probe mood_shift    : {moods}  -> shifted {shifted}/{runs}"
        f"  {'PASS' if result['probes']['mood_shift']['passes'] else 'FAIL'}")

    # Probe 2: fed the conflict directly, does the checker actually reject AND
    # supply a fallback the harness could legally commit?
    #
    # Two bars are tracked, because they disagree in practice:
    #   doc_bar  - Roadmap Phase 4's wording: verdict=reject and final_action
    #              differs from the proposed action.
    #   legal    - additionally, Section 3.4's "must supply a VALID fallback":
    #              final_action must be permitted for final_mood by the
    #              Section 4 table. reject/jump under mood alert clears the doc
    #              bar but is still forbidden, so it is not a real resolution.
    verdicts, overrode, legal_count, parsed_all = [], 0, 0, []
    proposed_action = T60_CONFLICT_PROPOSALS["proposed_action"]["action"]
    for _ in range(runs):
        try:
            _, text, _ = call(client, model, "checker", T60_STATE, T60_CONFLICT_PROPOSALS)
        except ModelUnavailable as exc:
            verdicts.append(f"ERROR:{exc}")
            continue
        obj = extract_json(text)
        parsed_all.append(obj)
        if not isinstance(obj, dict):
            verdicts.append("UNPARSEABLE")
            continue
        verdict = obj.get("verdict")
        final_mood = obj.get("final_mood")
        final_action = obj.get("final_action")

        in_enum = final_action in ACTIONS
        differs = final_action != proposed_action
        meets_doc_bar = verdict == "reject" and in_enum and differs
        is_legal = (
            in_enum
            and final_mood in MOODS
            # The probe's fixture has the character mid-celebrate, so the
            # smoothness rule applies as well as the pairing rule.
            and final_action in allowed_actions_for(final_mood, T60_STATE["current_action"])
        )
        if meets_doc_bar:
            overrode += 1
        if meets_doc_bar and is_legal:
            legal_count += 1

        if not in_enum:
            flag = "  <-NOT-IN-ENUM"
        elif not is_legal:
            flag = "  <-ILLEGAL-FALLBACK"
        else:
            flag = ""
        verdicts.append(f"{verdict}/{final_mood}/{final_action}{flag}")
    result["probes"]["checker_reject"] = {
        "runs": runs,
        "verdicts": verdicts,
        "parsed": parsed_all,
        "real_overrides": overrode,
        "legal_overrides": legal_count,
        "passes": legal_count >= (runs // 2 + 1),
    }
    cr = result["probes"]["checker_reject"]
    log(f"  probe checker_reject: {verdicts}")
    log(f"      -> meets doc bar {overrode}/{runs}, legal fallback {legal_count}/{runs}"
        f"  {'PASS' if cr['passes'] else 'FAIL'}")

    return result


def list_chat_models(client):
    models = []
    for entry in client.list().get("models", []):
        name = entry.get("model") or entry.get("name")
        if not name:
            continue
        if "embed" in name.lower():
            continue  # embedding models cannot do chat
        models.append(name)
    return sorted(models)


def print_summary(results, runs):
    print(f"\n{'=' * 78}\nSUMMARY ({runs} runs per measurement)\n{'=' * 78}")
    header = f"{'model':<22} {'tick':>7} {'valid':>7} {'mood_shift':>11} {'legal_fb':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        if not r["available"]:
            reason = (r["error"] or "")[:40]
            print(f"{r['model']:<22} UNAVAILABLE - {reason}")
            continue
        total_valid = sum(p["valid"] for p in r["personas"].values())
        total_runs = sum(p["runs"] for p in r["personas"].values())
        tick = f"{r['tick_total_s']:.1f}s" if r["tick_total_s"] else "n/a"
        ms = r["probes"]["mood_shift"]
        cr = r["probes"]["checker_reject"]
        valid_col = f"{total_valid}/{total_runs}"
        mood_col = f"{ms['shifted_off_excited']}/{runs} " + ("PASS" if ms["passes"] else "FAIL")
        check_col = f"{cr['legal_overrides']}/{runs} " + ("PASS" if cr["passes"] else "FAIL")
        print(f"{r['model']:<22} {tick:>7} {valid_col:>7} {mood_col:>11} {check_col:>9}")

    print("\nA model is demo-viable only if BOTH probes pass: the mood must move off")
    print("`excited` at t=60, and the checker must then override the stale celebrate")
    print("action with a fallback that is actually LEGAL for the final mood (an")
    print("override to `jump` clears Phase 4's wording but is still forbidden under")
    print("`alert`, so it does not count). Latency only decides whether timer-triggered")
    print("ticks are possible - it does not decide model choice (Section 7.1).")

    viable = [
        r["model"]
        for r in results
        if r["available"]
        and r["probes"]["mood_shift"]["passes"]
        and r["probes"]["checker_reject"]["passes"]
    ]
    print(f"\nBoth probes passed: {', '.join(viable) if viable else 'NONE'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=None, help="comma-separated; default = all installed chat models")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out", default=None, help="JSON artifact path")
    parser.add_argument("--keep-loaded", action="store_true", help="skip unloading between models")
    parser.add_argument(
        "--probes-only",
        action="store_true",
        help="skip the latency/validity sweep and run only the two decisive t=60 probes",
    )
    args = parser.parse_args()

    if args.runs < 1:
        print("FAIL: --runs must be at least 1.", file=sys.stderr)
        return 2

    client = ollama.Client(host=args.host, timeout=args.timeout)

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        try:
            models = list_chat_models(client)
        except (ConnectionError, httpx.ConnectError, ollama.RequestError) as exc:
            print(f"FAIL: could not reach Ollama at {args.host}.", file=sys.stderr)
            print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
            print("  Fix: start the server ('ollama serve') and retry.", file=sys.stderr)
            return 1

    if not models:
        print("FAIL: no chat models to benchmark.", file=sys.stderr)
        return 1

    print(f"host    : {args.host}")
    print(f"runs    : {args.runs} per measurement")
    print(f"models  : {', '.join(models)}")

    results = []
    for model in models:
        results.append(benchmark_model(client, model, args.runs, probes_only=args.probes_only))
        if not args.keep_loaded:
            unload(client, model)

    print_summary(results, args.runs)

    out_path = Path(args.out) if args.out else (
        REPO_ROOT / "tests" / "reliability" / f"benchmark_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "host": args.host,
                "runs": args.runs,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nartifact: {out_path}")

    if not any(r["available"] for r in results):
        print("FAIL: no model could be benchmarked.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
