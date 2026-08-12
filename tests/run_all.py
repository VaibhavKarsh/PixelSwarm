"""Run every check in the repo with one command.

Discovers tests/**/test_*.py and also runs the standalone validator scripts, so
"is the repo healthy?" is a single command rather than a remembered list. Each
file runs in its own process: they monkeypatch module globals, and sharing an
interpreter would let one file's patch leak into another's assertions.

    python tests/run_all.py
    python tests/run_all.py --quiet
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Validators first: if the config or prompts are wrong, the test failures that
# follow would be symptoms rather than causes.
VALIDATORS = [
    REPO_ROOT / "scripts" / "validate_repo_structure.py",
    REPO_ROOT / "scripts" / "validate_prompt_fidelity.py",
    REPO_ROOT / "scripts" / "validate_configs.py",
]


def discover_tests(include_live=False):
    """All test files, fast ones first.

    Files ending in `_live.py` need a running model server and take minutes
    against a 9b model, so they are excluded unless --live is passed. They are
    not skipped silently: main() prints what was left out.
    """
    found = sorted(
        (REPO_ROOT / "tests").rglob("test_*.py"),
        key=lambda p: (p.parent.name != "unit", str(p)),  # unit tests first
    )
    if include_live:
        return found
    return [p for p in found if not p.name.endswith("_live.py")]


def run(path, quiet):
    result = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    ok = result.returncode == 0
    label = path.relative_to(REPO_ROOT)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok or not quiet:
        tail = [ln for ln in (result.stdout or "").splitlines() if ln.startswith("OK") or ln.startswith("FAIL")]
        for line in tail[-1:]:
            print(f"      {line}")
    if not ok:
        print((result.stdout or "").rstrip())
        print((result.stderr or "").rstrip(), file=sys.stderr)
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run *_live.py, which need a running model server and take minutes",
    )
    args = parser.parse_args(argv)

    targets = VALIDATORS + discover_tests(include_live=args.live)
    excluded = [] if args.live else [p.name for p in (REPO_ROOT / "tests").rglob("test_*_live.py")]

    print(f"running {len(targets)} check(s)\n" + "-" * 68)
    results = [run(path, args.quiet) for path in targets]
    print("-" * 68)
    if excluded:
        print(f"note: skipped {', '.join(excluded)} (needs a live model; use --live)")

    failed = results.count(False)
    if failed:
        print(f"FAIL: {failed} of {len(results)} check(s) failed.", file=sys.stderr)
        return 1
    print(f"OK: all {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
