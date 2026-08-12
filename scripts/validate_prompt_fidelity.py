"""Guard against prompt drift: code prompts must match Architecture doc Section 3.5.

The Roadmap's loudest repeated warning is that a coding session will quietly
"improve" a persona prompt. Nothing actually checked that, and it had already
happened once: benchmark_models.py used a hyphen where Section 3.5 has an em
dash, which no test would have caught.

This compares every persona prompt defined in code against the fenced blocks in
Section 3.5, character for character. Run it after touching either side.

The transition-checker prompt is special: Section 3.5 carries the placeholder
`{{insert current contents of transitions.json here}}` where code injects the
real rules, so that one is checked as prefix + suffix around the injection point.

Usage:
    python scripts/validate_prompt_fidelity.py
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "docs" / "02_ARCHITECTURE_HARNESS_SPEC.md"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

CHECKER_PLACEHOLDER = "{{insert current contents of transitions.json here}}"

# Section 3.5 heading label -> attribute name in the module under test.
PROMPT_MAP = {
    "Mood-picker": "MOOD_PROMPT",
    "Action-picker": "ACTION_PROMPT",
    "Dialogue-line": "LINE_PROMPT",
    "Transition-checker": "CHECKER_PROMPT",
}

# Every module that holds a copy of the Section 3.5 prompts. swarm/personas.py
# is the one that reaches a real model from Phase 3 onward, so it matters most;
# the benchmark keeps its own copy so it can run standalone, and the two must not
# be allowed to drift apart.
TARGETS = [
    ("swarm/personas.py", "swarm.personas", "CHECKER_PROMPT_TEMPLATE"),
    ("scripts/benchmark_models.py", "benchmark_models", "CHECKER_PROMPT"),
]


def extract_doc_prompts():
    """Pull the four fenced prompt blocks out of Section 3.5."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.find("## 3.5 Concrete prompt templates")
    if start == -1:
        raise SystemExit("FAIL: could not find Section 3.5 in the spec.")
    # Stop at 3.5a (the rationale subsection) or 3.6, whichever comes first.
    ends = [i for i in (text.find("### 3.5a", start), text.find("## 3.6", start)) if i != -1]
    if not ends:
        raise SystemExit("FAIL: could not find the end of Section 3.5.")
    section = text[start:min(ends)]

    found = {}
    pattern = re.compile(
        r"\*\*(?P<name>[A-Za-z-]+) system prompt:\*\*.*?\n```\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    for match in pattern.finditer(section):
        found[match.group("name")] = match.group("body")
    return found


def compare(label, doc_prompt, code_prompt):
    """Return a list of problem strings (empty means identical)."""
    if code_prompt is None:
        return [f"{label}: not defined in code"]

    doc_norm = doc_prompt.strip()
    code_norm = code_prompt.strip()

    if CHECKER_PLACEHOLDER in doc_norm:
        prefix, _, suffix = doc_norm.partition(CHECKER_PLACEHOLDER)
        problems = []
        if prefix.strip() and prefix.strip() not in code_norm:
            problems.append(f"{label}: text BEFORE the transitions injection differs from the doc")
        if suffix.strip() and suffix.strip() not in code_norm:
            problems.append(f"{label}: text AFTER the transitions injection differs from the doc")
        return problems

    if doc_norm == code_norm:
        return []

    # Point at the first differing line so the fix is obvious.
    doc_lines, code_lines = doc_norm.splitlines(), code_norm.splitlines()
    for i in range(max(len(doc_lines), len(code_lines))):
        d = doc_lines[i] if i < len(doc_lines) else "<missing>"
        c = code_lines[i] if i < len(code_lines) else "<missing>"
        if d != c:
            return [
                f"{label}: first difference at line {i + 1}\n"
                f"      doc : {d!r}\n"
                f"      code: {c!r}"
            ]
    return [f"{label}: differs from the doc"]


def main() -> int:
    doc_prompts = extract_doc_prompts()

    missing_in_doc = [n for n in PROMPT_MAP if n not in doc_prompts]
    if missing_in_doc:
        print(f"FAIL: Section 3.5 is missing prompt block(s): {', '.join(missing_in_doc)}", file=sys.stderr)
        return 1

    import importlib

    print(f"spec    : {SPEC.name} Section 3.5")
    print("-" * 60)

    problems = []
    for path, module_name, checker_attr in TARGETS:
        print(f"  {path}")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            problems.append(f"{path}: could not import ({exc})")
            print(f"    FAIL  import: {exc}")
            continue

        for label, attr in PROMPT_MAP.items():
            if label == "Transition-checker":
                # Compare the RENDERED prompt - what the model actually receives.
                # personas.py stores a str.format template whose JSON braces are
                # doubled, so comparing the raw template would report false drift.
                render = getattr(module, "checker_prompt", None)
                value = render({}) if callable(render) else getattr(module, checker_attr, None)
            else:
                value = getattr(module, attr, None)
            errs = compare(label, doc_prompts[label], value)
            print(f"    {'OK  ' if not errs else 'DRIFT'}  {label}")
            problems.extend(f"{path}: {e}" for e in errs)

    # The enums are stated twice - in the prompt text and in config/personas.json.
    # A mismatch there is the same class of bug as prompt drift.
    config = json.loads((REPO_ROOT / "config" / "personas.json").read_text(encoding="utf-8-sig"))
    personas = importlib.import_module("swarm.personas")
    for key, where in (("moods", "MOOD_PROMPT"), ("actions", "ACTION_PROMPT")):
        listed = ", ".join(config[key])
        ok = listed in getattr(personas, where)
        if not ok:
            problems.append(f"{key}: config list ({listed!r}) does not appear verbatim in {where}")
        print(f"  {'OK  ' if ok else 'DRIFT'}  config {key} enum echoed in swarm/personas.py {where}")

    # --- semantic coverage --------------------------------------------------
    # Prompt fragility on this project has a single shape: an enum value is
    # LISTED as allowed but never given a trigger, so the model picks it
    # arbitrarily and the gap only surfaces when a scenario happens to hit it.
    # Four such gaps were found by luck across Phases 3-8; this catches the
    # class at test time instead. Being listed in "Allowed moods (choose exactly
    # one): ..." does not count as guidance, so that line is stripped first.
    print("  semantic coverage (every enum value needs a stated trigger)")
    for key, attr in (("moods", "MOOD_PROMPT"), ("actions", "ACTION_PROMPT")):
        body = "\n".join(
            line for line in getattr(personas, attr).splitlines()
            if not line.startswith("Allowed ")
        )
        missing = [v for v in config[key] if not re.search(rf"\b{re.escape(v)}\b", body)]
        ok = not missing
        print(f"    {'OK  ' if ok else 'GAP '}  {key}: {len(config[key]) - len(missing)}/{len(config[key])} have guidance in {attr}")
        if missing:
            problems.append(
                f"{attr}: {missing} are allowed values with no stated trigger — the model "
                f"will pick them arbitrarily. Either say when each applies, or remove them "
                f"from the enum."
            )

    print("-" * 60)
    if problems:
        print(f"FAIL: {len(problems)} prompt fidelity problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nSection 3.5 is the source of truth. Change the doc first, then the code.", file=sys.stderr)
        return 1

    print("OK: all persona prompts match Architecture doc Section 3.5 verbatim.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
