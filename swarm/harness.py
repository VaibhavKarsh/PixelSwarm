"""The tick loop (Architecture doc Section 1) and trace logging (Section 6).

Phase 2. The loop is real; the personas it calls are mocks from swarm/personas.py
and there are no model calls anywhere in this path. Phase 3 swaps the personas
out one at a time without changing this file's control flow.

The loop, verbatim from Section 1:

    1. gather current_state + new events since the last tick
    2. mood-picker         -> proposed_mood
    3. action-picker       -> proposed_action   (sees proposed_mood)
    4. dialogue-line       -> proposed_line     (sees mood + action)
    5. transition-checker  -> approve | reject + fallback
    6. commit final_state
    7. emit a Directive to the compiler adapter
    8. log the whole tick

Personas are called sequentially, never in parallel (Section 1). Because this is
a single-process script (Section 9), ticks cannot overlap by construction, which
is how Section 7's "skip a timer tick if one is already in flight" is satisfied.

Usage:
    python -m swarm.harness                 # full demo sequence, simulated clock
    python -m swarm.harness --realtime      # sleep between events, as a demo run
    python -m swarm.harness --events path   # a different scripted sequence
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarm import personas as persona_impls  # noqa: E402
from swarm.state import (  # noqa: E402
    banned_actions,
    legal_actions,
    load_events,
    state_from_config,
)
from swarm.version import config_fingerprint, fingerprint_details  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"
EVENTS_DEFAULT = REPO_ROOT / "events" / "demo_sequence.json"
LOG_DIR = REPO_ROOT / "logs"

PERSONA_KEYS = ("mood", "action", "line", "check")


class PersonaFailure(Exception):
    """A persona did not return a usable proposal (Section 5)."""


def load_json(path):
    # utf-8-sig for the same reason scripts/validate_configs.py uses it: Windows
    # editors write a BOM and plain utf-8 turns that into a baffling parse error.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_config():
    return (
        load_json(CONFIG_DIR / "personas.json"),
        load_json(CONFIG_DIR / "transitions.json"),
    )


# --- validation of persona output (Section 5) --------------------------------

def _require(condition, message):
    if not condition:
        raise PersonaFailure(message)


def validate_proposal(kind, payload, config):
    """Reject anything the harness could not safely commit.

    Section 5, as clarified 2026-07-30: a well-formed response carrying an
    out-of-enum value is a persona FAILURE, not a usable proposal. Phase 3 adds
    JSON parsing in front of this; the enum checks here do not change.
    """
    _require(isinstance(payload, dict), f"{kind}: response is not an object")
    moods, actions = config.get("moods", []), config.get("actions", [])

    if kind == "mood":
        _require("mood" in payload, "mood: missing key 'mood'")
        _require(payload["mood"] in moods, f"mood: out_of_enum:{payload['mood']!r}")
    elif kind == "action":
        _require("action" in payload, "action: missing key 'action'")
        _require(payload["action"] in actions, f"action: out_of_enum:{payload['action']!r}")
    elif kind == "line":
        _require("line" in payload, "line: missing key 'line'")
        _require(
            payload["line"] is None or isinstance(payload["line"], str),
            f"line: wrong type {type(payload['line']).__name__}",
        )
        # A model asked for "a short line or null" sometimes writes the WORD
        # null. It is a valid string, so it passed every check and reached the
        # renderer as a speech bubble reading "null" - caught by the Phase 8
        # alt-scenario run, not by any test, because the canonical run happened
        # never to trigger it. Normalise the near-misses to real silence.
        if isinstance(payload["line"], str):
            if payload["line"].strip().strip('"').lower() in ("", "null", "none", "nil", "n/a"):
                payload = {**payload, "line": None}
    elif kind == "check":
        for key in ("verdict", "final_mood", "final_action"):
            _require(key in payload, f"check: missing key {key!r}")
        _require(
            payload["verdict"] in ("approve", "reject"),
            f"check: bad verdict {payload['verdict']!r}",
        )
        _require(payload["final_mood"] in moods, f"check: out_of_enum final_mood:{payload['final_mood']!r}")
        _require(payload["final_action"] in actions, f"check: out_of_enum final_action:{payload['final_action']!r}")
    return payload


def describe_failure(exc):
    """Stable, greppable reason string for the trace log's `errors` field.

    docs/06_TESTING_STRATEGY.md Phase 3 is explicit that a timeout must not be
    collapsed into the same bucket as a parse failure - they call for entirely
    different fixes, and a run that reports only "error" tells you nothing. The
    prefixes here are the vocabulary the reliability report aggregates on.
    """
    if isinstance(exc, PersonaFailure):
        return str(exc)
    reason = getattr(exc, "reason", None)  # ModelTimeout, ModelUnavailable, ...
    if reason:
        return f"{reason}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def allowed_actions_for(mood, transitions, actions, current_action=None):
    """Legal actions given both Section 4 rule kinds. Delegates to state.py so
    the harness, the mock checker and the validator share one implementation."""
    return legal_actions(transitions, mood, current_action, actions)


# --- the tick ----------------------------------------------------------------

class Harness:
    def __init__(self, config, transitions, trace_path=None, sink=None,
                 scenario=None, mode=None):
        self.config = config
        self.transitions = transitions
        self.state = state_from_config(config)
        self.tick_number = 0
        self.directives = []
        self.sink = sink
        self.trace_path = Path(trace_path) if trace_path else None
        self._trace_fh = None
        self.fallback_action = config.get("fallback", {}).get("action", "idle_loop")
        # Section 5's second invariant. Configurable so the two behaviours stay
        # measurable against each other; the fingerprint below captures which
        # one produced a given trace.
        self.enforce_grounded_rejections = config.get("enforce_grounded_rejections", True)
        # Section 6: stamp every record with the config that produced it, so two
        # traces are never silently incomparable. Computed once - the config
        # cannot change mid-run.
        self.scenario = scenario
        self.mode = mode
        self.config_version = config_fingerprint(config, transitions)

    # -- trace log (Section 6) --

    def __enter__(self):
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            self._trace_fh = self.trace_path.open("w", encoding="utf-8")
            # The 12-char stamp on each record is only useful if it resolves to
            # something. This sidecar is what it resolves to.
            meta = fingerprint_details(
                self.config, self.transitions, scenario=self.scenario, mode=self.mode
            )
            self.trace_path.with_suffix(".meta.json").write_text(
                json.dumps(meta, indent=2) + "\n", encoding="utf-8"
            )
        return self

    def __exit__(self, *exc):
        if self._trace_fh:
            self._trace_fh.close()
            self._trace_fh = None
        return False

    def _write_trace(self, record):
        if self._trace_fh:
            # One JSON object per line, flushed per tick so a crashed run still
            # leaves a readable partial trace to debug from.
            self._trace_fh.write(json.dumps(record) + "\n")
            self._trace_fh.flush()

    # -- one persona call, with the Section 5 fallback --

    def _call(self, kind, fn, errors, timings, *args):
        started = time.perf_counter()
        try:
            payload = validate_proposal(kind, fn(*args, config=self.config), self.config)
            errors[kind] = None
            return payload
        except Exception as exc:  # noqa: BLE001 - any failure is a persona failure
            # Section 5: substitute a safe default, log the failure explicitly,
            # never retry indefinitely and never crash the tick loop.
            errors[kind] = describe_failure(exc)
            return None
        finally:
            timings[kind] = round((time.perf_counter() - started) * 1000, 1)

    def run_tick(self, trigger):
        """One full pass of Section 1's loop. Never raises."""
        self.tick_number += 1
        errors = {k: None for k in PERSONA_KEYS}
        timings = {k: 0.0 for k in PERSONA_KEYS}
        input_state = self.state.snapshot()

        # Steps 2-4. A failed persona yields the Section 5 default, which is
        # still only a PROPOSAL - it goes through the checker like any other.
        mood = self._call("mood", persona_impls.mood_picker, errors, timings, self.state)
        if mood is None:
            mood = {"mood": self.state.current_mood, "confidence": 0.0, "reason": "fallback: persona failed"}

        action = self._call("action", persona_impls.action_picker, errors, timings, self.state, mood)
        if action is None:
            action = {"action": self.fallback_action, "confidence": 0.0, "reason": "fallback: persona failed"}

        line = self._call("line", persona_impls.dialogue_line, errors, timings, self.state, mood, action)
        if line is None:
            line = {"line": None, "reason": "fallback: persona failed"}

        proposals = {"mood": mood, "action": action, "line": line}

        # Step 5.
        verdict = self._call(
            "check", persona_impls.transition_checker, errors, timings,
            self.state, proposals, self.transitions,
        )
        if verdict is None:
            # Section 5's last resort. Note it pins idle_loop rather than reusing
            # current_action, which at t=60 would be the forbidden `celebrate`.
            verdict = {
                "verdict": "harness_fallback",
                "final_mood": mood["mood"],
                "final_action": self.fallback_action,
                "final_line": None,
                "reason": "transition-checker failed; harness-level fallback",
            }

        final_mood = verdict["final_mood"]
        final_action = verdict["final_action"]
        final_line = verdict.get("final_line")
        previous_action = input_state.current_action

        # Section 5 invariant, second direction: never accept a rejection the
        # rules table does not support. Measured 2026-08-11, 72% of the checker's
        # rejections were of a LEGAL action, justified by rules it invented
        # ("Duck disallowed after idle_loop" - there is no such rule). Those
        # rejections were invisible to every acceptance criterion, because the
        # substituted idle_loop is itself legal, so the invariant below stayed
        # silent and the run still passed.
        #
        # The argument that makes this decidable rather than a matter of taste:
        # EVERY rule in Section 4 forbids an action. `by_mood[mood]` and
        # `by_previous_action[action]` both hold `disallowed_next_action` lists,
        # and there is no rule kind that can forbid a mood or a line. So if the
        # rejected action is legal, no rule in the table could have justified
        # rejecting it - the veto cites nothing, and is overruled.
        #
        # Scope is deliberately narrow. The mood is left exactly as the checker
        # decided it: the table cannot adjudicate moods, so overruling one would
        # be the harness inventing policy rather than enforcing the spec.
        # Legality is judged against the mood actually being committed, so the
        # restored action is legal by construction and the invariant below is a
        # no-op after it.
        if (
            self.enforce_grounded_rejections
            and verdict["verdict"] == "reject"
            # A harness_fallback is the checker having FAILED, not having judged.
            # Section 5's last resort must not be second-guessed.
            and errors["check"] is None
            # Only protect a decision the action-picker actually made. If it
            # failed, `action` is a harness-invented default and there is no
            # swarm judgement here to defend.
            and errors["action"] is None
        ):
            proposed_action = action["action"]
            if (
                proposed_action != final_action
                and proposed_action not in banned_actions(
                    self.transitions, final_mood, previous_action
                )
            ):
                verdict = {
                    **verdict,
                    "overruled": {
                        "checker_action": final_action,
                        "restored_action": proposed_action,
                        "reason": (
                            f"rejection not backed by the rules table: "
                            f"{proposed_action!r} is legal for mood {final_mood!r} "
                            f"coming out of {previous_action!r}"
                        ),
                    },
                }
                final_action = proposed_action

        # Section 5 invariant: never commit an action the table disallows,
        # whatever any persona said. Both Section 4 rule kinds apply, and the
        # smoothness rule is evaluated against the action the character is
        # animating GOING INTO this tick - input_state, not the value we are
        # about to commit.
        banned = banned_actions(self.transitions, final_mood, previous_action)
        if final_action in banned:
            legal = legal_actions(
                self.transitions, final_mood, previous_action, self.config.get("actions", [])
            )
            substitute = self.fallback_action if self.fallback_action in legal else (legal[0] if legal else "idle_loop")
            errors["check"] = (errors["check"] or "") + (
                f" | harness invariant: {final_action!r} is disallowed for {final_mood!r}, "
                f"substituted {substitute!r}"
            ).strip(" |")
            final_action = substitute

        # Step 6.
        self.state.commit(final_mood, final_action, final_line)

        # Step 7 - Directive per Interface Contract doc Section 3.1. The adapter
        # itself is Phase 5; this only produces the payload and hands it to a sink.
        directive = {
            "tick": self.tick_number,
            "mood": final_mood,
            "action": final_action,
            "line": final_line,
            "ts": trigger.get("ts"),
        }
        self.directives.append(directive)
        if self.sink:
            self.sink(directive)

        # Step 8.
        record = {
            "tick": self.tick_number,
            "config_version": self.config_version,
            "trigger": trigger,
            "input_state": input_state.to_prompt_dict(),
            "proposals": proposals,
            "errors": errors,
            "verdict": verdict,
            "final_state": self.state.to_prompt_dict(),
            "timing_ms": timings,
        }
        self._write_trace(record)
        return record

    # -- the run --

    def run(self, events, tick_timer_s=None, idle_ticks=2, realtime=False, on_tick=None):
        """Replay `events`, firing one tick per event plus trailing idle ticks.

        Ticks are strictly sequential: an event never starts a tick while another
        is running, satisfying Section 7's no-overlap requirement by construction
        rather than by locking.
        """
        timer_s = tick_timer_s if tick_timer_s is not None else self.config.get("tick_timer_s", 15)
        records = []
        clock = 0.0
        started_wall = time.monotonic()

        for event in events:
            if realtime:
                # Sleep until this event is due relative to the START of the run,
                # not to the previous event. With real models a tick can outlast
                # the gap to the next event (config/personas.json notes ~26.6s
                # ticks against 15s spacing), and sleeping per-gap would let the
                # schedule drift by the whole accumulated tick time. If we are
                # already late, do not sleep - replay falls behind, which is the
                # documented behaviour: wait for the in-flight tick, never
                # overlap it (Architecture doc Section 7).
                behind = event.ts - (time.monotonic() - started_wall)
                if behind > 0:
                    time.sleep(behind)
            clock = max(clock, event.ts)
            self.state.add_event(event, now_ts=clock)
            record = self.run_tick({"type": "event", "event_type": event.type, "ts": event.ts})
            records.append(record)
            if on_tick:
                on_tick(record)

        # Trailing timer ticks demonstrate the loop settles and holds a stable
        # idle state rather than only reacting (Demo Script acceptance criteria,
        # Functional item 5). No new events arrive, so the window ages out.
        for _ in range(idle_ticks):
            clock += timer_s
            if realtime:
                time.sleep(timer_s)
            self.state.prune(clock)
            record = self.run_tick({"type": "timer", "event_type": None, "ts": clock})
            records.append(record)
            if on_tick:
                on_tick(record)

        return records


PERSONA_NAMES = ("mood", "action", "line", "checker")

# `--real` and config/personas.json name the fourth persona "checker"; the trace
# log's `errors`/`timing_ms` call it "check" (Section 6). The two vocabularies
# are both load-bearing and cannot be merged without breaking either the config
# schema or the trace format, so the correspondence is written down here rather
# than re-derived. Getting it wrong is silent: `errors.get("checker")` is always
# None, so a checker failure simply vanishes from any count keyed by name.
ERROR_KEY_FOR = {"mood": "mood", "action": "action", "line": "line", "checker": "check"}


def parse_real_personas(value):
    """Turn --real into the list of personas that use a real model."""
    if not value:
        return []
    normalised = value.strip().lower()
    if normalised == "all":
        return list(PERSONA_NAMES)
    # `none` spelled out, because an empty string is awkward to pass from
    # PowerShell and reads as an accident rather than an intent.
    if normalised in ("none", "mock", "mocked"):
        return []
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in PERSONA_NAMES]
    if unknown:
        raise SystemExit(
            f"FAIL: unknown persona(s) for --real: {', '.join(unknown)}. "
            f"Valid: {', '.join(PERSONA_NAMES)}, or 'all'."
        )
    # Canonical order, not the order typed. This is a membership list, so order
    # never mattered behaviourally - but it reaches the config fingerprint
    # (swarm/version.py), and `--real mood,checker` must not hash differently
    # from `--real checker,mood`.
    return [n for n in PERSONA_NAMES if n in names]


def new_trace_path():
    return LOG_DIR / f"trace_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", default=str(EVENTS_DEFAULT))
    parser.add_argument("--trace", default=None, help="trace log path (default logs/trace_<ts>.jsonl)")
    parser.add_argument("--idle-ticks", type=int, default=2)
    parser.add_argument("--realtime", action="store_true", help="sleep between events instead of simulating the clock")
    parser.add_argument(
        "--real",
        default="",
        help="comma-separated personas to run against a REAL model: mood,action,line,checker "
             "(or 'all', or 'none'). Default is none - every persona mocked, as in Phase 2. "
             "Phase 3 enables these one at a time so a breakage is attributable to one change.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    config, transitions = load_config()
    config["real_personas"] = parse_real_personas(args.real)
    events = load_events(load_json(args.events))
    trace_path = Path(args.trace) if args.trace else new_trace_path()

    real = config["real_personas"]
    if not args.quiet:
        print(f"events : {args.events} ({len(events)} event(s))")
        print(f"trace  : {trace_path}")
        if real:
            models = ", ".join(f"{p}={config['models'].get(p, '?')}" for p in real)
            mocked = [p for p in PERSONA_NAMES if p not in real]
            print(f"real   : {models}")
            print(f"mocked : {', '.join(mocked) if mocked else 'none'}")
        else:
            print("models : ALL MOCKED (no model calls)")
        print("-" * 72)

    def report(record):
        if args.quiet:
            return
        final = record["final_state"]
        flag = "" if record["verdict"]["verdict"] == "approve" else f"  <-{record['verdict']['verdict'].upper()}"
        trig = record["trigger"]["event_type"] or "timer"
        line = f' "{final["last_line"]}"' if final["last_line"] else ""
        print(f"  tick {record['tick']:>2}  {trig:<16} -> {final['current_mood']:<8} {final['current_action']:<12}{line}{flag}")

    with Harness(
        config, transitions, trace_path=trace_path,
        scenario=Path(args.events).name,
        mode=",".join(real) if real else "mocked",
    ) as harness:
        records = harness.run(events, idle_ticks=args.idle_ticks, realtime=args.realtime, on_tick=report)

    if not args.quiet:
        print("-" * 72)
        moods = {r["final_state"]["current_mood"] for r in records}
        actions = {r["final_state"]["current_action"] for r in records}
        overrides = [r["tick"] for r in records if r["verdict"]["verdict"] != "approve"]
        failures = sum(1 for r in records for v in r["errors"].values() if v)
        print(f"ticks       : {len(records)}")
        print(f"moods seen  : {sorted(moods)}")
        print(f"actions seen: {sorted(actions)}")
        print(f"overrides   : {overrides if overrides else 'none'}")
        print(f"persona failures: {failures}")
        print(f"\ntrace log: {trace_path}")

    # Section 5 requires the tick loop to survive any persona failure, and it
    # does - which means a run with NO model behind it still completes, exits 0,
    # and prints a tidy summary of a character that never moved. For someone
    # trying the project for the first time that reads as "it works and does
    # nothing", which is the worst possible first impression and is the reason
    # this check exists. The harness behaviour is untouched; only the CLI's
    # verdict on the run changes.
    if real and records:
        attempted = len(records) * len(real)
        failed = sum(
            1 for r in records for name in real if r["errors"].get(ERROR_KEY_FOR[name])
        )
        if failed == attempted:
            host = config.get("runtime", {}).get("host", "?")
            print(
                f"\nFAIL: every model call failed ({failed}/{attempted}).\n"
                f"  No model is reachable at {host}.\n"
                f"  - is Ollama running?           ollama serve\n"
                f"  - is the model pulled?         ollama pull {config['models'].get('mood', '?')}\n"
                f"  - check the host/model names in config/personas.json\n"
                f"  - or drop --real to run the mocked personas, which need nothing.\n"
                f"  The trace above is a real run of the fallback path, not a working swarm.",
                file=sys.stderr,
            )
            return 1
        if failed:
            print(
                f"\nWARNING: {failed} of {attempted} model calls failed; the affected ticks "
                f"used Section 5 fallbacks. Check `errors` in the trace.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
