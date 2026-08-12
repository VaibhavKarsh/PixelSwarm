"""Phase 5 tests: the adapter, and the boundary it is supposed to protect.

The load-bearing test here is `test_pixel_world_imports_nothing_from_the_swarm`.
PRD success criterion 4 asks that the compiler render the animation "without
modification to its core rendering logic". Since the renderer had to be written
here (the Pixel-World Compiler never existed - Architecture doc Section 8), that
criterion cannot be proven the intended way: you cannot show you did not modify
code you wrote yourself.

What IS provable is the criterion's intent - the renderer has no knowledge of the
swarm, so the swarm cannot have reached into it. That is checked mechanically
below rather than asserted in prose, which is the whole point.

    python tests/integration/test_adapter.py
"""

import ast
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from compiler_adapter.adapter import (  # noqa: E402
    DirectiveError,
    RenderAdapter,
    directive_to_state,
    states_from_trace,
    validate_directive,
)
from pixel_world import ACTIONS, MOODS, Renderer  # noqa: E402
from swarm.harness import Harness, load_config, load_json  # noqa: E402
from swarm.state import load_events  # noqa: E402

CONFIG, TRANSITIONS = load_config()
CANONICAL = REPO_ROOT / "demo" / "trace_canonical.jsonl"


def directive(mood="alert", action="duck", line=None, tick=1):
    return {"tick": tick, "mood": mood, "action": action, "line": line, "ts": 60.0}


# --- the boundary --------------------------------------------------------------

def _imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_pixel_world_imports_nothing_from_the_swarm():
    """The criterion that replaces 'no changes to compiler core'.

    If the renderer imported from swarm/, the two systems would be one system
    with a naming convention, and the Interface Contract would be decorative.
    """
    for path in sorted((REPO_ROOT / "pixel_world").rglob("*.py")):
        for name in _imports_of(path):
            root = name.split(".")[0]
            assert root not in ("swarm", "compiler_adapter"), (
                f"{path.name} imports {name!r} — the renderer must not know about "
                f"the swarm or the adapter"
            )


def _code_identifiers(path):
    """Identifiers and runtime strings, EXCLUDING docstrings and comments.

    A docstring saying "this knows nothing about personas" is correct
    documentation, not a boundary violation — an earlier version of this test
    checked raw text and duly failed on the renderer explaining itself.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                found.add(node.value)
    return {f.lower() for f in found}


def test_pixel_world_code_never_references_swarm_concepts():
    """Belt and braces beyond the import check: no swarm vocabulary in the code
    itself. Docstrings are exempt — the renderer is allowed to say what it is
    not."""
    banned = ("persona", "directive", "transition_checker", "mood_picker", "tick_timer")
    for path in sorted((REPO_ROOT / "pixel_world").rglob("*.py")):
        identifiers = _code_identifiers(path)
        for word in banned:
            hits = [i for i in identifiers if word in i]
            assert not hits, f"{path.name} code references {word!r}: {hits}"


def test_the_adapter_is_the_only_module_bridging_both_sides():
    adapter_imports = _imports_of(REPO_ROOT / "compiler_adapter" / "adapter.py")
    assert any(i.split(".")[0] == "pixel_world" for i in adapter_imports)
    # And nothing in swarm/ reaches across to the renderer.
    for path in sorted((REPO_ROOT / "swarm").rglob("*.py")):
        for name in _imports_of(path):
            assert name.split(".")[0] != "pixel_world", f"{path.name} imports {name!r}"


# --- validation (resolves 06 Section 4 ambiguity #2) ---------------------------

def test_valid_directive_passes():
    assert validate_directive(directive()) is not None


def test_every_enum_combination_is_renderable():
    """Catches 'forgot to add the mapping for duck' at test time rather than
    during a demo (docs/06_TESTING_STRATEGY.md Phase 5)."""
    renderer = Renderer()
    for mood in MOODS:
        for action in ACTIONS:
            state = directive_to_state(directive(mood, action))
            assert state["mood"] == mood and state["action"] == action
            img = renderer.render(mood, action)
            assert img.size[0] > 0 and img.size[1] > 0


def test_unknown_mood_is_rejected():
    try:
        validate_directive(directive(mood="neutral"))
    except DirectiveError as exc:
        assert "neutral" in str(exc)
    else:
        raise AssertionError("an unknown mood must not reach the renderer")


def test_unknown_action_is_rejected():
    try:
        validate_directive(directive(action="moonwalk"))
    except DirectiveError as exc:
        assert "moonwalk" in str(exc)
    else:
        raise AssertionError("an unknown action must not reach the renderer")


def test_missing_key_is_rejected():
    for key in ("mood", "action"):
        payload = directive()
        del payload[key]
        try:
            validate_directive(payload)
        except DirectiveError as exc:
            assert key in str(exc)
        else:
            raise AssertionError(f"missing {key} must be rejected")


def test_non_dict_is_rejected():
    for bad in ("alert", 42, None, ["alert"]):
        try:
            validate_directive(bad)
        except DirectiveError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be rejected")


def test_wrong_typed_line_is_rejected():
    try:
        validate_directive(directive(line=7))
    except DirectiveError as exc:
        assert "line" in str(exc)
    else:
        raise AssertionError("a non-string line must be rejected")


def test_null_line_is_accepted():
    assert directive_to_state(directive(line=None))["line"] is None


def test_empty_line_becomes_none():
    # An empty bubble would be drawn as an empty box, which reads as a bug.
    assert directive_to_state(directive(line=""))["line"] is None


# --- the adapter ---------------------------------------------------------------

def test_undrawable_characters_are_stripped_from_speech():
    """A real run produced "Yay!! <party popper>" and the emoji drew as a tofu
    box. A missing glyph reads as a rendering bug, not as text."""
    assert Renderer.drawable("Yay!! \U0001F389") == "Yay!!"
    assert Renderer.drawable("Hey there!") == "Hey there!"
    assert Renderer.drawable("café") == "café", "Latin-1 is drawable"


def test_an_emoji_only_line_becomes_silence_not_an_empty_bubble():
    for only in ("\U0001F389", "\U0001F389 \U0001F600", "", None):
        assert Renderer.drawable(only) is None


def test_adapter_holds_the_last_directive():
    """Interface Contract Section 4: a Directive stays active until replaced."""
    adapter = RenderAdapter()
    adapter.send(directive(action="wave"))
    adapter.send(directive(action="duck"))
    assert adapter.last_directive["action"] == "duck"
    assert len(adapter.states) == 2


def test_adapter_renders_the_held_directive():
    adapter = RenderAdapter()
    assert adapter.frame() is None, "nothing sent yet"
    adapter.send(directive())
    assert adapter.frame() is not None


def test_adapter_rejects_a_bad_directive_rather_than_holding_it():
    adapter = RenderAdapter()
    adapter.send(directive(action="wave"))
    try:
        adapter.send(directive(action="moonwalk"))
    except DirectiveError:
        pass
    else:
        raise AssertionError("bad directive must raise")
    assert adapter.last_directive["action"] == "wave", "state must be unchanged"
    assert len(adapter.states) == 1


def test_adapter_works_as_a_live_harness_sink():
    """The same call path a real run uses, so rendering from a trace afterwards
    is not a different integration from driving it live."""
    adapter = RenderAdapter()
    events = load_events(load_json(REPO_ROOT / "events" / "demo_sequence.json"))
    with Harness(CONFIG, TRANSITIONS, sink=adapter.send) as harness:
        records = harness.run(events, idle_ticks=1)
    assert len(adapter.states) == len(records)
    for state, record in zip(adapter.states, records):
        assert state["mood"] == record["final_state"]["current_mood"]
        assert state["action"] == record["final_state"]["current_action"]


# --- trace rendering -----------------------------------------------------------

def test_states_from_trace_marks_overrides():
    records = [json.loads(l) for l in CANONICAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    adapter = states_from_trace(records)
    assert len(adapter.states) == len(records)
    badged = [i for i, s in enumerate(adapter.states) if s["badge"]]
    rejected = [i for i, r in enumerate(records) if r["verdict"]["verdict"] == "reject"]
    assert badged == rejected, "the OVERRIDE badge must mark exactly the rejects"


def test_canonical_trace_renders_to_a_gif():
    records = [json.loads(l) for l in CANONICAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    adapter = states_from_trace(records)
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.gif"
        # Small and fast: this asserts the pipeline works, not how it looks.
        frames = adapter.write_gif(out, scale=2, frames_per_state=2, fps=12)
        assert frames == len(records) * 2
        assert out.stat().st_size > 0


def test_rendering_with_no_directives_raises():
    try:
        RenderAdapter().write_gif("unused.gif")
    except DirectiveError:
        pass
    else:
        raise AssertionError("rendering nothing must raise, not write an empty file")


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
    print(f"OK: {len(tests)} adapter tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
