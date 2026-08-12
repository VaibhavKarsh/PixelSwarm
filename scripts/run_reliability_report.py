"""Phase 4/6 reliability report (docs/06_TESTING_STRATEGY.md Section 5).

Runs the full demo sequence N times with all four personas real, scores every
Demo Script Functional acceptance criterion per run, and aggregates. This is what
turns Phase 6's "8 out of 10 runs pass" from a manual tally into one command, and
what gives the portfolio writeup a real number instead of an anecdote.

06 Section 5 is explicit that this must exist BEFORE further tuning: at n=3-4 the
criteria swing enough that successive prompt edits were observed trading them
against each other indistinguishably from noise (Architecture doc Section 7.1e).
Every question left in Phase 4 is "is this rate actually different?", and n>=10 is
the minimum that can answer it.

Criteria scored (Demo Script doc, Section 2 Functional):
  C1  the run completes with no crash, hang or unhandled exception
  C2  at least 3 distinct moods AND 3 distinct actions are observed
  C3  the t=60 tick shows a genuine transition-checker override
  C4  chat_calm at t=15 produces no change
  C5  the run ends in a stable idle state

C3 deliberately requires a REAL checker reject, not a harness-level fallback: a
fallback fires when the checker FAILED, which is the opposite of demonstrating
that the swarm resolved a conflict. It also requires the substituted action to be
legal, since Phase 4's own wording ("final_action differs") admits a false pass -
see 06 Section 4.

Usage:
    python scripts/run_reliability_report.py                 # n=10, all real
    python scripts/run_reliability_report.py --runs 3
    python scripts/run_reliability_report.py --real ""       # mocked, for a fast self-check
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarm.harness import Harness, load_config, load_json, parse_real_personas  # noqa: E402
from swarm.state import banned_actions, load_events  # noqa: E402
from swarm.version import config_fingerprint  # noqa: E402

OUT_DIR = REPO_ROOT / "tests" / "reliability"

CRITERIA = {
    "C1": "run completes without crashing",
    "C2": "3+ distinct moods and 3+ distinct actions",
    "C3": "genuine checker override at t=60",
    "C4": "chat_calm at t=15 produces no change",
    "C5": "run ends in a stable idle state",
}


def score_run(records, transitions, config):
    """Score one run's trace. Returns (results dict, per-criterion detail)."""
    detail = {}

    detail["C1"] = f"{len(records)} ticks"
    c1 = bool(records)

    moods = {r["final_state"]["current_mood"] for r in records}
    actions = {r["final_state"]["current_action"] for r in records}
    c2 = len(moods) >= 3 and len(actions) >= 3
    detail["C2"] = f"{len(moods)} moods, {len(actions)} actions"

    t60 = next((r for r in records if r["trigger"]["ts"] == 60.0), None)
    if t60 is None:
        c3, detail["C3"] = False, "no tick at t=60"
    else:
        verdict = t60["verdict"]
        proposed = t60["proposals"]["action"]["action"]
        final = verdict.get("final_action")
        previous = t60["input_state"]["current_action"]
        legal = final not in banned_actions(
            transitions, verdict.get("final_mood"), previous
        )
        # A harness fallback means the checker FAILED - the opposite of the swarm
        # resolving a conflict - so it must not count as the demo's beat.
        genuine = verdict.get("verdict") == "reject" and t60["errors"]["check"] is None
        c3 = bool(genuine and final != proposed and legal)
        # Record what was actually COMMITTED as well as what the checker said.
        # They differ whenever the harness invariant had to substitute, and
        # without both you cannot tell "the checker resolved the conflict" from
        # "the checker got it wrong and the deterministic backstop saved it" -
        # which are very different stories for the portfolio writeup.
        committed = t60["final_state"]["current_action"]
        detail["C3"] = (
            f"{verdict.get('verdict')} {previous}->{final} (proposed {proposed})"
            f"{'' if legal else ' ILLEGAL'}"
            f"{'' if committed == final else f' committed={committed}'}"
            f"{'' if t60['errors']['check'] is None else ' [checker failed]'}"
        )
        detail["C3_committed"] = committed
        detail["C3_intervened"] = committed != proposed

    t15 = next((r for r in records if r["trigger"]["ts"] == 15.0), None)
    if t15 is None:
        c4, detail["C4"] = False, "no tick at t=15"
    else:
        before = (t15["input_state"]["current_mood"], t15["input_state"]["current_action"])
        after = (t15["final_state"]["current_mood"], t15["final_state"]["current_action"])
        c4 = before == after
        detail["C4"] = f"{before[0]}/{before[1]} -> {after[0]}/{after[1]}"

    last = records[-1]["final_state"] if records else {}
    c5 = last.get("current_mood") == "idle" and last.get("current_action") == "idle_loop"
    detail["C5"] = f"{last.get('current_mood')}/{last.get('current_action')}"

    failures = Counter()
    for record in records:
        for persona, reason in record["errors"].items():
            if reason:
                failures[f"{persona}:{reason.split(':')[0]}"] += 1

    # Grounding: of the rejections the checker made, how many rejected an action
    # the table actually forbids? The five criteria cannot see this - a rejection
    # of a LEGAL action still commits idle_loop, which is legal, so the harness
    # invariant stays silent and every criterion can still pass. Measured on
    # 2026-08-11, 72% of rejections across 20 runs were of a legal action, with
    # confabulated reasons ("Wave is disallowed when mood is excited due to alert
    # mood constraint"). That is the project's central claim - decisions grounded
    # in the rules table - failing quietly, so it is now reported every run.
    rejections = ungrounded = 0
    for record in records:
        if record["verdict"]["verdict"] != "reject":
            continue
        proposal = record["proposals"]["action"]
        if not isinstance(proposal, dict) or not proposal.get("action"):
            continue  # the action persona failed; there was nothing to reject
        rejections += 1
        forbidden = banned_actions(
            transitions,
            record["verdict"].get("final_mood") or record["final_state"]["current_mood"],
            record["input_state"]["current_action"],
        )
        if proposal["action"] not in forbidden:
            ungrounded += 1
    detail["rejections"] = rejections
    detail["ungrounded_rejections"] = ungrounded
    # How many of those the Section 5 guard actually overruled. With the guard
    # on this tracks `ungrounded` closely but is not identical: the guard stands
    # down when the checker itself failed or the action-picker failed, so the
    # gap between the two columns is exactly the set of ungrounded rejections
    # that were still honoured, and is worth being able to see.
    detail["overruled"] = sum(
        1 for r in records if "overruled" in r["verdict"]
    )

    return (
        {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5},
        detail,
        failures,
        {"moods": sorted(moods), "actions": sorted(actions)},
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--real", default="all", help="personas to run for real; '' = all mocked")
    parser.add_argument("--events", default=str(REPO_ROOT / "events" / "demo_sequence.json"))
    parser.add_argument("--idle-ticks", type=int, default=2)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep-traces", action="store_true", help="also write each run's trace to logs/")
    args = parser.parse_args(argv)

    if args.runs < 1:
        print("FAIL: --runs must be at least 1.", file=sys.stderr)
        return 2

    config, transitions = load_config()
    config["real_personas"] = parse_real_personas(args.real)
    events = load_events(load_json(args.events))

    print(f"runs   : {args.runs}")
    print(f"real   : {', '.join(config['real_personas']) if config['real_personas'] else 'none (all mocked)'}")
    print(f"events : {args.events} ({len(events)} events)")
    # Every figure this script prints is "measured under some configuration".
    # Print which one, so a number copied into a doc stays attributable and a
    # later run that disagrees can be checked for a config change first.
    print(f"config : {config_fingerprint(config, transitions)}")
    print("-" * 78)

    runs, all_failures = [], Counter()
    for i in range(1, args.runs + 1):
        trace_path = (
            REPO_ROOT / "logs" / f"reliability_{time.strftime('%Y%m%d_%H%M%S')}_{i}.jsonl"
            if args.keep_traces else None
        )
        started = time.monotonic()
        crashed = None
        try:
            with Harness(
                config, transitions, trace_path=trace_path,
                scenario=Path(args.events).name,
                mode=",".join(config.get("real_personas") or []) or "mocked",
            ) as harness:
                records = harness.run(events, idle_ticks=args.idle_ticks)
        except Exception as exc:  # noqa: BLE001
            # C1 exists to catch exactly this; a crashed run is a scored failure,
            # not a reason to abandon the report.
            crashed, records = f"{type(exc).__name__}: {exc}", []
        elapsed = time.monotonic() - started

        if crashed:
            results = {k: False for k in CRITERIA}
            detail, failures, seen = {"C1": crashed}, Counter(), {"moods": [], "actions": []}
        else:
            results, detail, failures, seen = score_run(records, transitions, config)
        all_failures.update(failures)

        passed = sum(results.values())
        flags = " ".join(f"{k}{'+' if v else '-'}" for k, v in results.items())
        print(f"  run {i:>2}  {flags}   {passed}/5   {elapsed:>5.1f}s"
              f"   {detail.get('C3', '')[:46]}")
        runs.append({
            "run": i, "results": results, "detail": detail,
            "elapsed_s": round(elapsed, 1),
            "failures": dict(failures), "seen": seen,
            "crashed": crashed,
        })

    print("-" * 78)
    print(f"{'criterion':<44} {'pass rate':>10}")
    print("-" * 78)
    totals = {}
    for key, label in CRITERIA.items():
        passed = sum(1 for r in runs if r["results"][key])
        totals[key] = passed
        bar = "#" * passed + "." * (args.runs - passed)
        print(f"  {key} {label:<40} {passed:>2}/{args.runs}  {bar}")

    all_pass = sum(1 for r in runs if all(r["results"].values()))
    print("-" * 78)
    print(f"  runs passing ALL criteria: {all_pass}/{args.runs}"
          f"   (Phase 6 bar: {int(args.runs * 0.8)}/{args.runs})")

    # C3 is strict: it counts only a genuine checker reject. Report separately how
    # often the illegal action was kept OUT of the committed state by any means,
    # since that is the safety property, whereas C3 is the demo's narrative beat.
    intervened = sum(1 for r in runs if r["detail"].get("C3_intervened"))
    print(f"  t=60 intervened at all (checker OR harness invariant): {intervened}/{args.runs}")

    # Grounding, aggregated. This is not one of the five criteria and cannot be:
    # a wrongly-rejected LEGAL action still commits idle_loop, so every criterion
    # can pass while the checker's stated reasons are fiction.
    rej = sum(r["detail"].get("rejections", 0) for r in runs)
    ungrounded = sum(r["detail"].get("ungrounded_rejections", 0) for r in runs)
    if rej:
        affected = sum(1 for r in runs if r["detail"].get("ungrounded_rejections"))
        print(f"  checker rejections not backed by the rules table: "
              f"{ungrounded}/{rej} ({100 * ungrounded / rej:.0f}%), "
              f"affecting {affected}/{args.runs} runs")
        overruled = sum(r["detail"].get("overruled", 0) for r in runs)
        # The rate above measures the MODEL and must keep doing so - the guard
        # fixes the outcome, not the checker's reasoning, and hiding the rate
        # once the guard is on would discard the only signal that says whether
        # the underlying behaviour ever improves.
        print(f"    of those, overruled by the Section 5 guard: {overruled}"
              f"   (still honoured: {ungrounded - overruled})")

    total_failures = sum(all_failures.values())
    per_run = total_failures / args.runs
    print(f"  persona failures: {total_failures} total, {per_run:.2f}/run"
          f"   (Phase 6 bar: <=1/run)")
    if all_failures:
        for key, count in all_failures.most_common():
            print(f"      {key:<34} {count}")

    durations = [r["elapsed_s"] for r in runs]
    if durations:
        print(f"  tick-loop wall time: mean {statistics.mean(durations):.1f}s, "
              f"min {min(durations):.1f}s, max {max(durations):.1f}s")

    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runs": args.runs,
        "real_personas": config["real_personas"],
        "criteria": CRITERIA,
        "totals": totals,
        "all_pass": all_pass,
        "failures_per_run": round(per_run, 2),
        "detail": runs,
    }, indent=2), encoding="utf-8")
    print(f"\nartifact: {out_path}")

    # Exit 0 even when criteria fail: this is a measurement tool, and a report
    # showing 6/10 is a successful measurement. Only a broken report is failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
