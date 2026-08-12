"""Phase 1 config check: the three JSON files parse, agree with each other, and
agree with the docs that define them.

Roadmap Phase 1 asks for "loads all three JSON files and checks they parse and
that every type in demo_sequence.json is one you intend to handle."
docs/06_TESTING_STRATEGY.md Sections 2.2 and 3 ask for more than that, and this
implements the fuller list:

  - duplicate keys inside any JSON object (json.loads silently keeps the last
    one, so a typo'd duplicate would otherwise pass unnoticed)
  - every transitions.json mood/action actually exists in personas.json
  - no mood is left with zero legal actions
  - every demo_sequence event type is in the locked Interface Contract enum
  - intensity within 0..1, ts non-decreasing, required keys present

The locked enums are PARSED OUT OF THE DOCS rather than copied here, so this
script cannot drift from them the way scripts/benchmark_models.py silently
drifted from Section 3.5. Sources of truth:
  - moods/actions            -> docs/02_ARCHITECTURE_HARNESS_SPEC.md Section 3.5
  - event types              -> docs/03_COMPILER_INTERFACE_CONTRACT.md Section 2.1

Every failure names the file and the field. Exit code 0 = clean, 1 = problems.

Usage:
    python scripts/validate_configs.py
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PERSONAS = REPO_ROOT / "config" / "personas.json"
TRANSITIONS = REPO_ROOT / "config" / "transitions.json"
SEQUENCE = REPO_ROOT / "events" / "demo_sequence.json"
SPEC = REPO_ROOT / "docs" / "02_ARCHITECTURE_HARNESS_SPEC.md"
CONTRACT = REPO_ROOT / "docs" / "03_COMPILER_INTERFACE_CONTRACT.md"

PERSONA_NAMES = ["mood", "action", "line", "checker"]

# Interface Contract Section 2.1, corrected 2026-07-31 (was 1.0). Presence-only
# signals carry no magnitude, but the value is not free: the mood prompt reads
# intensity as strength, so 1.0 made `chat_calm` arrive as a maximum-strength
# event and cost acceptance criterion 4 across four model configurations.
PRESENCE_ONLY_INTENSITY = 0.1


class ConfigError(Exception):
    """Fatal: cannot continue checking (file missing or unparseable)."""


def rel(path):
    """Repo-relative path for messages, tolerating paths outside the repo
    (the unit tests point these constants at a temp directory)."""
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def _no_duplicate_keys(pairs):
    """object_pairs_hook that rejects duplicate keys instead of silently
    keeping the last value."""
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in the same JSON object")
        seen[key] = value
    return seen


def load(path):
    if not path.is_file():
        raise ConfigError(f"{rel(path)}: file does not exist")
    try:
        # utf-8-sig, not utf-8: PowerShell's Set-Content -Encoding utf8 and
        # several Windows editors prepend a BOM, which plain utf-8 surfaces as a
        # baffling "Unexpected UTF-8 BOM" JSON error. Tolerate it silently.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"{rel(path)}: cannot read: {exc}") from exc
    if not text.strip():
        raise ConfigError(f"{rel(path)}: file is empty (expected JSON)")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        raise ConfigError(f"{rel(path)}: invalid JSON: {exc}") from exc


def public_keys(mapping):
    """Config keys, ignoring the _-prefixed documentation ones."""
    return {k for k in mapping if not k.startswith("_")}


# --- doc parsing (source of truth) -------------------------------------------

def doc_enum(label):
    """Read 'Allowed moods (choose exactly one): a, b, c' out of Section 3.5."""
    text = SPEC.read_text(encoding="utf-8-sig")
    match = re.search(rf"Allowed {label} \(choose exactly one\): (.+)", text)
    if not match:
        raise ConfigError(f"could not find the allowed-{label} line in {SPEC.name} Section 3.5")
    return [v.strip() for v in match.group(1).split(",") if v.strip()]


def doc_demo_table(event_types):
    """Parse the Demo Script's Section 1 sequence table into (ts, type, intensity).

    Phase 1's Definition of Done says to eyeball demo_sequence.json against this
    table, and Interface Contract Section 6 calls a mismatch here the most common
    source of "nothing happens" bugs. Eyeballing does not survive edits, so it is
    automated instead.

    Rows carry the event name either backticked (`chat_calm`) or bare in
    parentheses ((idle_timeout)); rows with no event at all ("(none - idle
    start)") yield nothing. Intensity is read from "(intensity 0.6)" when present.
    """
    demo = REPO_ROOT / "docs" / "04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md"
    text = demo.read_text(encoding="utf-8-sig")
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        ts_cell = cells[0].strip("` ")
        try:
            ts = float(ts_cell)
        except ValueError:
            continue  # header or separator row
        found = [t for t in event_types if re.search(rf"\b{re.escape(t)}\b", cells[1])]
        if not found:
            continue  # e.g. "(none - idle start)"
        if len(found) > 1:
            raise ConfigError(
                f"{demo.name} Section 1: row ts={ts} names more than one event type: {found}"
            )
        intensity_match = re.search(r"intensity\s+([0-9]*\.?[0-9]+)", cells[1])
        intensity = float(intensity_match.group(1)) if intensity_match else None
        rows.append((ts, found[0], intensity))
    return rows


def doc_event_types():
    """Read the locked event-type enum out of Interface Contract Section 2.1."""
    text = CONTRACT.read_text(encoding="utf-8-sig")
    anchor = text.find("- `type`: fixed enum for v1")
    if anchor == -1:
        raise ConfigError(f"could not find the event-type enum line in {CONTRACT.name} Section 2.1")
    match = re.search(r"\[(?:\s*\"[a-z_]+\"\s*,?)+\]", text[anchor:anchor + 800])
    if not match:
        raise ConfigError(f"could not parse the event-type array in {CONTRACT.name} Section 2.1")
    return json.loads(match.group(0))


# --- checks ------------------------------------------------------------------

def check_personas(cfg, doc_moods, doc_actions, errors):
    where = "config/personas.json"
    if not isinstance(cfg, dict):
        errors.append(f"{where}: top level must be a JSON object")
        return None, None

    for field, expected in (("moods", doc_moods), ("actions", doc_actions)):
        value = cfg.get(field)
        if not isinstance(value, list) or not value:
            errors.append(f"{where}: '{field}' must be a non-empty list")
            continue
        if not all(isinstance(v, str) for v in value):
            errors.append(f"{where}: '{field}' must contain only strings")
            continue
        if len(set(value)) != len(value):
            dupes = sorted({v for v in value if value.count(v) > 1})
            errors.append(f"{where}: '{field}' has duplicate entries: {dupes}")
        if value != expected:
            errors.append(
                f"{where}: '{field}' does not match Architecture doc Section 3.5.\n"
                f"      config: {value}\n"
                f"      doc   : {expected}\n"
                f"      The prompt is the source of truth - a mismatch means the model is told "
                f"one enum while the harness validates against another."
            )

    moods = cfg.get("moods") if isinstance(cfg.get("moods"), list) else []
    actions = cfg.get("actions") if isinstance(cfg.get("actions"), list) else []

    models = cfg.get("models")
    if not isinstance(models, dict):
        errors.append(f"{where}: 'models' must be a JSON object with one entry per persona")
    else:
        missing = [p for p in PERSONA_NAMES if p not in models]
        if missing:
            errors.append(f"{where}: 'models' is missing persona(s): {missing}")
        unexpected = sorted(public_keys(models) - set(PERSONA_NAMES))
        if unexpected:
            errors.append(f"{where}: 'models' has unknown persona(s): {unexpected}")
        for name, model in models.items():
            if name.startswith("_"):
                continue
            if not isinstance(model, str) or not model.strip():
                errors.append(f"{where}: models.{name} must be a non-empty model name string")

    for field, minimum in (
        ("recent_events_max_count", 1),
        ("recent_events_max_age_s", 1),
        ("tick_timer_s", 1),
    ):
        value = cfg.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{where}: '{field}' must be a number (Architecture doc Sections 2 and 7)")
        elif value < minimum:
            errors.append(f"{where}: '{field}' must be >= {minimum}, got {value}")

    runtime = cfg.get("runtime")
    if not isinstance(runtime, dict):
        errors.append(f"{where}: 'runtime' must be a JSON object (Architecture doc Section 9)")
    else:
        if not runtime.get("provider"):
            errors.append(f"{where}: runtime.provider must name the local model runtime")
        if runtime.get("think") is not False:
            errors.append(
                f"{where}: runtime.think must be false. Architecture doc Section 7.1 measured a "
                f"~100x slowdown with reasoning enabled; this is not a tuning preference."
            )
        timeout = runtime.get("timeout_s")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            errors.append(
                f"{where}: runtime.timeout_s must be a positive number - the ollama client "
                f"defaults to NO timeout, so a stalled call would hang the tick loop forever."
            )

    fallback = cfg.get("fallback")
    if not isinstance(fallback, dict):
        errors.append(f"{where}: 'fallback' must be a JSON object (Architecture doc Section 5)")
    else:
        fb_action = fallback.get("action")
        if fb_action not in actions:
            errors.append(
                f"{where}: fallback.action {fb_action!r} is not in the actions enum - "
                f"the harness could never commit it"
            )
        if "line" in fallback and fallback["line"] is not None and not isinstance(fallback["line"], str):
            errors.append(f"{where}: fallback.line must be null or a string")

    return moods, actions


def _check_rule_section(rules, section, valid_keys, key_label, actions, errors):
    """Validate one of the two Section 4 rule sections."""
    where = "config/transitions.json"
    table = rules.get(section)
    if table is None:
        return {}
    if not isinstance(table, dict):
        errors.append(f"{where}: '{section}' must be a JSON object keyed by {key_label}")
        return {}

    for key, rule in table.items():
        if key.startswith("_"):
            continue
        if key not in valid_keys:
            errors.append(
                f"{where}: {section}.{key!r} is not a {key_label} in personas.json. "
                f"Known: {valid_keys}"
            )
        if not isinstance(rule, dict):
            errors.append(f"{where}: {section}.{key!r} must be an object")
            continue
        unknown_fields = sorted(public_keys(rule) - {"disallowed_next_action"})
        if unknown_fields:
            errors.append(f"{where}: {section}.{key!r} has unknown field(s): {unknown_fields}")
        banned = rule.get("disallowed_next_action")
        if not isinstance(banned, list):
            errors.append(f"{where}: {section}.{key!r}.disallowed_next_action must be a list")
            continue
        for act in banned:
            if act not in actions:
                errors.append(
                    f"{where}: {section}.{key!r} disallows {act!r}, which is not an action in "
                    f"personas.json. Known actions: {actions}"
                )
        if len(set(banned)) != len(banned):
            errors.append(f"{where}: {section}.{key!r}.disallowed_next_action has duplicate entries")
    return table


def check_transitions(rules, moods, actions, errors):
    """Architecture doc Section 4: two rule sections, both binding at once."""
    where = "config/transitions.json"
    if not isinstance(rules, dict):
        errors.append(f"{where}: top level must be a JSON object")
        return

    known_sections = {"by_mood", "by_previous_action"}
    present = public_keys(rules)
    unknown = sorted(present - known_sections)
    if unknown:
        errors.append(
            f"{where}: unknown top-level key(s): {unknown}. Expected only "
            f"{sorted(known_sections)} (Architecture doc Section 4). A flat mood-keyed "
            f"object is the pre-2026-07-30 format and is no longer read - its rules "
            f"would be silently ignored."
        )
    if not present & known_sections:
        errors.append(f"{where}: must define at least one of {sorted(known_sections)}")
        return

    by_mood = _check_rule_section(rules, "by_mood", moods, "mood", actions, errors)
    by_prev = _check_rule_section(rules, "by_previous_action", actions, "action", actions, errors)

    # Reachability, applying BOTH rule kinds together. A combination that forbids
    # every action would leave the harness with nothing legal to commit.
    for mood in moods:
        mood_banned = set((by_mood.get(mood) or {}).get("disallowed_next_action") or [])
        for prev in list(actions) + [None]:
            prev_banned = set((by_prev.get(prev) or {}).get("disallowed_next_action") or [])
            remaining = [a for a in actions if a not in (mood_banned | prev_banned)]
            if not remaining:
                errors.append(
                    f"{where}: mood {mood!r} coming out of action {prev!r} disallows every "
                    f"action - no state could ever be committed"
                )

    # The configured fallback must survive every combination, or Section 5's
    # invariant has nothing safe to substitute.
    fallback = "idle_loop"
    for mood in moods:
        mood_banned = set((by_mood.get(mood) or {}).get("disallowed_next_action") or [])
        if fallback in mood_banned:
            errors.append(
                f"{where}: by_mood.{mood!r} disallows the fallback action {fallback!r}. "
                f"Section 5 pins that as the always-legal commit; banning it breaks the "
                f"harness invariant's last resort."
            )
    for prev, rule in (by_prev or {}).items():
        if prev.startswith("_"):
            continue
        if fallback in set((rule or {}).get("disallowed_next_action") or []):
            errors.append(
                f"{where}: by_previous_action.{prev!r} disallows the fallback action "
                f"{fallback!r}, which Section 5 requires to always be committable."
            )


# The transition-checker prompt embeds the ENTIRE rules table verbatim, so the
# table is not just config - it is prompt length, and prompt length is the thing
# small models fail on. This is measured, not assumed: `granite4.1:3b` scored 8/8
# on the checker probe under a one-rule table and 0/8 under two (see the
# `_checker_split_note` in config/personas.json). Nothing capped the growth, so a
# future session could add rules until the checker quietly degrades, with no test
# failing and the reliability figures moving for an unattributed reason.
#
# These are budgets, not correctness limits. Exceeding one is not a bug - it is a
# signal to RE-MEASURE the checker at the new size and then raise the budget
# deliberately, the same discipline the golden trace uses for regeneration.
MAX_TRANSITION_RULES = 8          # currently 5
MAX_CHECKER_PROMPT_CHARS = 2600   # currently 1828 rendered


def check_rules_table_budget(rules, errors):
    where = "config/transitions.json"
    count = sum(
        len(public_keys(rules.get(section) or {}))
        for section in ("by_mood", "by_previous_action")
    )
    if count > MAX_TRANSITION_RULES:
        errors.append(
            f"{where}: {count} rules exceeds the budget of {MAX_TRANSITION_RULES}. The "
            f"checker prompt embeds this whole table, and small models measurably "
            f"degrade as it grows (granite4.1:3b: 8/8 at one rule, 0/8 at two). "
            f"Re-measure the checker at this size, then raise MAX_TRANSITION_RULES "
            f"in this file with the new numbers."
        )

    # The rule count is a proxy; the rendered prompt is what the model actually
    # reads, so budget that too - a few rules with long action lists cost more
    # than several short ones.
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from swarm.personas import checker_prompt
    except ImportError:  # swarm/ not importable (Phase 0/1) - the count check still ran
        return
    rendered = len(checker_prompt(rules))
    if rendered > MAX_CHECKER_PROMPT_CHARS:
        errors.append(
            f"{where}: the rendered transition-checker prompt is {rendered} characters, "
            f"over the {MAX_CHECKER_PROMPT_CHARS} budget. Same remedy as above: "
            f"re-measure, then raise MAX_CHECKER_PROMPT_CHARS deliberately."
        )


def check_sequence(events, event_types, errors):
    where = "events/demo_sequence.json"
    if not isinstance(events, list):
        errors.append(f"{where}: top level must be a JSON array of events")
        return

    # An empty sequence is structurally valid (the harness should run zero ticks
    # cleanly), so it is a warning rather than an error - see 06 Phase 1 edge cases.
    previous_ts = None
    for i, event in enumerate(events):
        tag = f"{where}[{i}]"
        if not isinstance(event, dict):
            errors.append(f"{tag}: each event must be a JSON object")
            continue

        etype = event.get("type")
        if etype is None:
            errors.append(f"{tag}: missing required key 'type'")
        elif etype not in event_types:
            errors.append(
                f"{tag}: type {etype!r} is not in the locked enum from Interface Contract "
                f"Section 2.1: {event_types}"
            )

        intensity = event.get("intensity")
        if intensity is None:
            errors.append(f"{tag}: missing required key 'intensity'")
        elif isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
            errors.append(f"{tag}: 'intensity' must be a number, got {type(intensity).__name__}")
        elif not 0.0 <= float(intensity) <= 1.0:
            errors.append(f"{tag}: 'intensity' must be within 0.0-1.0, got {intensity}")

        ts = event.get("ts")
        if ts is None:
            errors.append(f"{tag}: missing required key 'ts'")
        elif isinstance(ts, bool) or not isinstance(ts, (int, float)):
            errors.append(f"{tag}: 'ts' must be a number, got {type(ts).__name__}")
        elif ts < 0:
            errors.append(f"{tag}: 'ts' must be >= 0, got {ts}")
        elif previous_ts is not None and ts < previous_ts:
            errors.append(
                f"{tag}: 'ts' {ts} is earlier than the previous event's {previous_ts}. "
                f"Events must be listed in non-decreasing ts order (Interface Contract "
                f"Section 2.1); ties keep file order."
            )
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            previous_ts = ts

        meta = event.get("meta")
        if meta is not None and not isinstance(meta, dict):
            errors.append(f"{tag}: 'meta' must be an object when present")


def check_sequence_matches_demo_script(events, table, errors):
    """demo_sequence.json must reproduce the Demo Script table exactly."""
    where = "events/demo_sequence.json"
    if not isinstance(events, list):
        return  # already reported by check_sequence

    actual = [(float(e["ts"]), e["type"], e.get("intensity"))
              for e in events
              if isinstance(e, dict)
              and isinstance(e.get("ts"), (int, float)) and not isinstance(e.get("ts"), bool)
              and isinstance(e.get("type"), str)]

    if len(actual) != len(table):
        errors.append(
            f"{where}: has {len(actual)} usable event(s) but the Demo Script Section 1 table "
            f"lists {len(table)}.\n"
            f"      file : {[(t, n) for t, n, _ in actual]}\n"
            f"      table: {[(t, n) for t, n, _ in table]}"
        )
        return

    for i, ((a_ts, a_type, a_int), (d_ts, d_type, d_int)) in enumerate(zip(actual, table)):
        if a_ts != d_ts or a_type != d_type:
            errors.append(
                f"{where}[{i}]: is ts={a_ts} {a_type!r} but the Demo Script table says "
                f"ts={d_ts} {d_type!r}"
            )
            continue
        if d_int is not None and a_int != d_int:
            errors.append(
                f"{where}[{i}]: ts={a_ts} {a_type!r} has intensity {a_int}, but the Demo "
                f"Script table specifies {d_int}"
            )
        if d_int is None and a_int != PRESENCE_ONLY_INTENSITY:
            errors.append(
                f"{where}[{i}]: ts={a_ts} {a_type!r} is a presence-only signal (the table "
                f"gives it no intensity), so it must use intensity {PRESENCE_ONLY_INTENSITY} "
                f"per Interface Contract Section 2.1, got {a_int}. That value is not "
                f"cosmetic: the mood prompt reads intensity as strength, so a high value "
                f"makes 'nothing is happening' arrive as the strongest possible signal."
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sequence",
        default=None,
        help="event file to validate (default: events/demo_sequence.json). Phase 8 adds a "
             "second scenario, and a scenario that cannot be validated is worse than none.",
    )
    args = parser.parse_args(argv)

    global SEQUENCE
    if args.sequence:
        SEQUENCE = Path(args.sequence)

    errors = []
    warnings = []

    try:
        doc_moods = doc_enum("moods")
        doc_actions = doc_enum("actions")
        event_types = doc_event_types()
        demo_table = doc_demo_table(event_types)
        personas = load(PERSONAS)
        transitions = load(TRANSITIONS)
        sequence = load(SEQUENCE)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"repo root : {REPO_ROOT}")
    print(f"moods     : {doc_moods}")
    print(f"actions   : {doc_actions}")
    print(f"event enum: {event_types}")
    print(f"demo table: {len(demo_table)} event(s) parsed from Demo Script Section 1")
    print("-" * 68)

    moods, actions = check_personas(personas, doc_moods, doc_actions, errors)
    if moods is not None:
        check_transitions(transitions, moods, actions, errors)
        check_rules_table_budget(transitions, errors)
    check_sequence(sequence, event_types, errors)

    # Table conformance is a claim about THE canonical demo sequence, not about
    # any event file. Phase 8 anticipates a second scenario, and the unit tests
    # feed synthetic sequences (empty, single-event) to exercise structural
    # handling - none of those should be forced to reproduce this table.
    canonical = (REPO_ROOT / "events" / "demo_sequence.json").resolve()
    if SEQUENCE.resolve() == canonical:
        check_sequence_matches_demo_script(sequence, demo_table, errors)
        print(f"  OK    checked against Demo Script Section 1 table ({len(demo_table)} events)")
    else:
        print("  SKIP  Demo Script table conformance (not the canonical sequence file)")

    if isinstance(sequence, list):
        if not sequence:
            warnings.append("events/demo_sequence.json is empty - the harness will run zero ticks")
        else:
            seen_ts = [e.get("ts") for e in sequence if isinstance(e, dict)]
            dupes = sorted({t for t in seen_ts if seen_ts.count(t) > 1 and t is not None})
            if dupes:
                warnings.append(
                    f"events/demo_sequence.json has events sharing a ts: {dupes}. "
                    f"Allowed (file order breaks the tie) but easy to author by accident."
                )
            used = {e.get("type") for e in sequence if isinstance(e, dict)}
            unused = sorted(set(event_types) - used)
            if unused:
                warnings.append(f"event types defined but never used in the sequence: {unused}")

    for path, label in ((PERSONAS, "personas"), (TRANSITIONS, "transitions"), (SEQUENCE, "sequence")):
        print(f"  OK    parsed {label:<11} {rel(path)}")

    print("-" * 68)

    for warning in warnings:
        print(f"  NOTE  {warning}")

    if errors:
        print(f"\nFAIL: {len(errors)} problem(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("\nOK: config and event files are valid and mutually consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
