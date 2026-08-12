"""demo/WRITEUP.md must agree with the artifact it cites.

The writeup's whole claim is that you do not have to take its word for anything:
it prints a tick table and a "verbatim" quote, and invites the reader to open
demo/trace_canonical.jsonl and check. That only works if the two actually agree.

They did not. A 2026-08-11 review found the writeup's tick table differed from
the trace in 4 of 10 rows, all three "verbatim" reason strings were paraphrases,
and the stated action variety was wrong. Both files had been regenerated in the
same commit; only one of them was fully resynced. Nothing checked, because prose
is not usually testable - but this prose is a set of claims about a JSON file,
which is exactly the kind of claim a machine can check.

The tick table is GENERATED here rather than parsed, so there is one source of
truth: if this test fails, paste `python tests/integration/test_writeup_matches_trace.py --print`
into the writeup.

Run:
    python tests/integration/test_writeup_matches_trace.py
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE = REPO_ROOT / "demo" / "trace_canonical.jsonl"
WRITEUP = REPO_ROOT / "demo" / "WRITEUP.md"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def records():
    return [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]


def tick_table(recs) -> str:
    """The 10-line summary block the writeup prints under '## The artifact'."""
    rows = []
    for r in recs:
        final = r["final_state"]
        trigger = r["trigger"]["event_type"] or "timer"
        line = f' "{final["last_line"]}"' if final["last_line"] else ""
        flag = "" if r["verdict"]["verdict"] == "approve" else "  <-" + r["verdict"]["verdict"].upper()
        # Without this the table contradicts itself: a row marked REJECT that
        # commits the proposed action anyway looks like a typo rather than
        # Section 5's second invariant doing its job.
        if "overruled" in r["verdict"]:
            flag += " (OVERRULED)"
        rows.append(
            f"{r['tick']:>2} {trigger:<16} {final['current_mood']:<8} "
            f"{final['current_action']:<12}{line}{flag}".rstrip()
        )
    return "\n".join(rows)


# --- the tick table ----------------------------------------------------------

def test_writeup_tick_table_matches_the_trace():
    expected = tick_table(records())
    body = WRITEUP.read_text(encoding="utf-8")
    if expected in body:
        return
    # Report the first differing row - "a block differs" is not actionable.
    printed = [l for l in body.splitlines() if re.match(r"^\s*\d+ (chat_|game_|idle_|timer)", l)]
    for i, (want, got) in enumerate(zip(expected.splitlines(), printed)):
        assert want == got, (
            f"writeup tick table row {i + 1} disagrees with the trace\n"
            f"      trace  : {want!r}\n"
            f"      writeup: {got!r}\n"
            f"    regenerate with: python {Path(__file__).relative_to(REPO_ROOT)} --print"
        )
    raise AssertionError(
        f"writeup tick table has {len(printed)} rows, trace has {len(expected.splitlines())}"
    )


# --- the "verbatim" quote ----------------------------------------------------

def test_every_quoted_reason_appears_in_the_trace():
    """The writeup calls this block 'Verbatim from trace_canonical.jsonl'."""
    body = WRITEUP.read_text(encoding="utf-8")
    start = body.find("Verbatim from")
    assert start != -1, "writeup no longer contains the 'Verbatim from' block"
    block = body[start:body.find("```", body.find("```", start) + 3)]

    reasons = set()
    for r in records():
        for proposal in r["proposals"].values():
            if isinstance(proposal, dict) and proposal.get("reason"):
                reasons.add(proposal["reason"])
        if r["verdict"].get("reason"):
            reasons.add(r["verdict"]["reason"])

    quoted = re.findall(r'"([^"]{12,})"', block)
    assert quoted, "no quoted reason strings found in the verbatim block"
    for q in quoted:
        assert q in reasons, (
            f"writeup quotes {q!r} as verbatim, but no proposal or verdict in the "
            f"trace has that reason. It is a paraphrase, not a quote."
        )


# --- claims about the trace's contents ---------------------------------------

def test_stated_mood_and_action_variety_is_correct():
    recs = records()
    actual = {
        "moods": len({r["final_state"]["current_mood"] for r in recs}),
        "actions": len({r["final_state"]["current_action"] for r in recs}),
    }
    body = WRITEUP.read_text(encoding="utf-8")
    # \s+ throughout: the writeup is hard-wrapped, so any of these gaps can be
    # a newline. Matching only spaces made a real mismatch look like a missing
    # sentence, which is a worse failure message than the bug it was hiding.
    match = re.search(
        r"(\w+)\s+distinct\s+moods?\s+and\s+(\w+)\s+distinct\s+actions?", body, re.IGNORECASE
    )
    assert match, "writeup no longer states the mood/action variety of the trace"
    claimed = {
        "moods": NUMBER_WORDS.get(match.group(1).lower()),
        "actions": NUMBER_WORDS.get(match.group(2).lower()),
    }
    assert claimed == actual, f"writeup claims {claimed}, trace has {actual}"


def test_stated_tick_count_matches():
    recs = records()
    body = WRITEUP.read_text(encoding="utf-8")
    match = re.search(r"one complete real run, (\w+) ticks", body)
    assert match, "writeup no longer states the trace's tick count"
    assert NUMBER_WORDS.get(match.group(1).lower()) == len(recs), \
        f"writeup says {match.group(1)} ticks, trace has {len(recs)}"


def test_the_trace_carries_a_config_version():
    """Section 6: the showcase artifact must be attributable like any other run."""
    for r in records():
        assert r.get("config_version"), (
            f"tick {r['tick']} has no config_version - this trace predates the "
            f"Section 6 schema and cannot be tied to a configuration."
        )


# --- claims about the demo assets --------------------------------------------

def test_stated_gif_durations_match_the_files():
    """README and 04 quote durations for the demo GIFs; the files decide.

    Found wrong on 2026-08-11: both docs said the full demo was "75 seconds"
    when the file is 69. Same failure mode as the tick table - a number typed
    once, then the artifact regenerated underneath it. The acceptance criterion
    is a RANGE (60-90 s), so the criterion still held and nothing flagged it.
    """
    try:
        from PIL import Image
    except ImportError:  # Pillow is renderer-only; the suite must run without it
        print("      (skipped: Pillow not installed)")
        return

    docs = {
        "README.md": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "docs/04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md": (
            REPO_ROOT / "docs" / "04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md"
        ).read_text(encoding="utf-8"),
    }

    checked = 0
    for name in ("pixel_swarm_demo_full.gif", "pixel_swarm_demo.gif"):
        with Image.open(REPO_ROOT / "demo" / name) as im:
            seconds = round(im.n_frames * im.info.get("duration", 0) / 1000)
        for where, text in docs.items():
            stated = [
                int(m) for m in re.findall(
                    rf"\|\s*`{re.escape(name)}`\s*\|\s*(\d+)\s*s\b", text
                )
            ] + [
                int(m) for m in re.findall(
                    rf"`demo/{re.escape(name)}`,\s*\*\*(\d+)\s*seconds", text
                )
            ]
            for claim in stated:
                assert abs(claim - seconds) <= 1, (
                    f"{where} says {name} is {claim}s; the file is {seconds}s"
                )
            checked += len(stated)

    # Without this the test passes when the docs simply stop stating durations -
    # which is exactly what happened when the README's artifact table was
    # removed in the 2026-08-11 rewrite. A guard that can quietly become a no-op
    # is worse than no guard, because it still reads as coverage.
    assert checked, (
        "no stated GIF duration found in README.md or 04 — either restore one, "
        "or delete this test rather than leaving it vacuously green"
    )


def test_readme_test_counts_are_current():
    """"15 checks, 259 tests" in the README has to still be true.

    It was not: the README said "11 checks, ~200 tests" and the write-up said
    10, long after the suite had grown to 15. Nobody notices a stale count,
    which is precisely why it should not be maintained by hand. Both numbers are
    derived here the same way tests/run_all.py derives them.
    """
    tests_dir = REPO_ROOT / "tests"
    files = [p for p in tests_dir.rglob("test_*.py") if not p.name.endswith("_live.py")]
    validators = 3  # run_all also runs the three scripts/validate_*.py checks
    checks = len(files) + validators
    functions = sum(
        len(re.findall(r"^def test_", p.read_text(encoding="utf-8"), re.M)) for p in files
    )

    seen = 0
    for rel in ("README.md", "demo/WRITEUP.md"):
        body = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for stated_checks, stated_tests in re.findall(r"(\d+)\s+checks,\s+(\d+)\s+tests", body):
            seen += 1
            assert int(stated_checks) == checks, \
                f"{rel} says {stated_checks} checks; the suite has {checks}"
            assert int(stated_tests) == functions, \
                f"{rel} says {stated_tests} tests; the suite has {functions}"
        # The README badge carries the number a second time, in a URL where it is
        # easy to miss. Both have gone stale before.
        for badge in re.findall(r"badge/tests-(\d+)-", body):
            seen += 1
            assert int(badge) == functions, \
                f"{rel} test badge says {badge}; the suite has {functions}"
    assert seen, "neither README nor the write-up states the check/test counts any more"


def main() -> int:
    if "--print" in sys.argv:
        print(tick_table(records()))
        return 0
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
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
