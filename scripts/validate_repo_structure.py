"""Phase 0 structure check: diff the actual repo tree against Architecture doc Section 8.

Exists so "the folder structure matches the spec" is a command, not a manual
read-through. Run from anywhere; paths resolve relative to the repo root.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_DIRS = [
    "config",
    "swarm",
    "events",
    "pixel_world",
    "compiler_adapter",
    "scripts",
    "tests/unit",
    "tests/integration",
    "tests/fixtures",
    "tests/reliability",
    "logs",
    "demo",
    "docs",
]

REQUIRED_FILES = [
    "README.md",
    "requirements.txt",
    ".gitignore",
    "config/personas.json",
    "config/transitions.json",
    "swarm/harness.py",
    "swarm/personas.py",
    "swarm/state.py",
    "swarm/model_client.py",
    "events/demo_sequence.json",
    "events/alt_sequence.json",
    "compiler_adapter/adapter.py",
    "scripts/smoke_test_model.py",
    "scripts/validate_repo_structure.py",
    "scripts/benchmark_models.py",
    "scripts/validate_prompt_fidelity.py",
    "scripts/validate_configs.py",
    "scripts/run_reliability_report.py",
    "tests/run_all.py",
    "tests/unit/test_config_validation.py",
    "tests/unit/test_state.py",
    "tests/unit/test_json_extraction.py",
    "tests/unit/test_harness_transitions.py",
    "tests/integration/test_harness_loop.py",
    "tests/integration/test_persona_fallbacks.py",
    "tests/integration/test_real_persona_paths.py",
    "tests/integration/test_personas_live.py",
    "tests/integration/test_golden_trace.py",
    "tests/integration/test_adapter.py",
    "pixel_world/__init__.py",
    "pixel_world/renderer.py",
    "scripts/render_trace.py",
    "tests/fixtures/fake_model_client.py",
    "tests/fixtures/golden_trace_mocked.jsonl",
    "tests/fixtures/sample_trace_real.jsonl",
    "demo/WRITEUP.md",
    "demo/RECORDING_SHOTLIST.md",
    "demo/trace_canonical.jsonl",
    "demo/pixel_swarm_demo.gif",
    "demo/pixel_swarm_demo_full.gif",
    "demo/pixel_swarm_explained.mp4",
    "scripts/render_explained.py",
    "scripts/render_logo.py",
    # The README embeds these two, so a missing file is a broken front page.
    # Committed rather than generated on demand: GitHub renders the README
    # without running anything, and nothing else in the repo needs Pillow.
    "demo/logo.png",
    "demo/banner.png",
    # Not part of Section 8's tree, but required for a public repo: without it
    # the default is all-rights-reserved and nobody may legally reuse the code.
    # REQUIRED rather than merely allowed, so deleting it fails a check.
    "LICENSE",
    # The six planning docs, moved out of the repo root 2026-08-11 so the front
    # page reads as a project rather than a filing cabinet. They are REQUIRED,
    # not merely allowed: three of them are parsed at validation time as the
    # source of truth for the enums, the prompts and the demo table, so losing
    # one breaks a validator rather than just a link.
    "docs/01_PRD.md",
    "docs/02_ARCHITECTURE_HARNESS_SPEC.md",
    "docs/03_COMPILER_INTERFACE_CONTRACT.md",
    "docs/04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md",
    "docs/05_IMPLEMENTATION_ROADMAP.md",
    "docs/06_TESTING_STRATEGY.md",
]

# Present in the working directory by design, but not part of Section 8's tree.
ALLOWED_EXTRA_TOP_LEVEL = {
    ".git",
    ".gitignore",
    ".gitattributes",
    ".claude",
    ".github",   # CI workflow, added 2026-08-11
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
}


def main() -> int:
    missing_dirs = [d for d in REQUIRED_DIRS if not (REPO_ROOT / d).is_dir()]
    missing_files = [f for f in REQUIRED_FILES if not (REPO_ROOT / f).is_file()]

    expected_top_level = {d.split("/")[0] for d in REQUIRED_DIRS}
    expected_top_level |= {f for f in REQUIRED_FILES if "/" not in f}
    expected_top_level |= ALLOWED_EXTRA_TOP_LEVEL

    unexpected = sorted(
        entry.name
        for entry in REPO_ROOT.iterdir()
        if entry.name not in expected_top_level
    )

    print(f"repo root: {REPO_ROOT}")
    print("-" * 60)

    for d in REQUIRED_DIRS:
        print(f"  {'OK  ' if (REPO_ROOT / d).is_dir() else 'MISS'}  {d}/")
    for f in REQUIRED_FILES:
        print(f"  {'OK  ' if (REPO_ROOT / f).is_file() else 'MISS'}  {f}")

    print("-" * 60)

    if unexpected:
        print("NOTE: top-level entries not in Architecture doc Section 8:")
        for name in unexpected:
            print(f"  - {name}")
        print("  (informational - add to Section 8 or to ALLOWED_EXTRA_TOP_LEVEL if intended)")

    if missing_dirs or missing_files:
        print()
        print(f"FAIL: {len(missing_dirs)} missing directory(s), {len(missing_files)} missing file(s).")
        return 1

    print("OK: structure matches Architecture doc Section 8.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
