"""Render a trace log to an animated GIF (Phase 5 / Phase 7).

Turns a trace produced by the harness into the visual artifact PRD success
criterion 4 asks for. Rendering from the trace rather than driving the renderer
live is deliberate:

  - it is deterministic and reproducible, which a screen capture never is;
  - it removes the pacing problem entirely (a real run takes ~5.5 minutes for a
    130-second sequence, because each tick makes four model calls - GIF frame
    timing is just a number);
  - the Directive path is identical either way, so nothing about the integration
    is faked. `RenderAdapter.send` is the same call the harness makes live.

Usage:
    python scripts/render_trace.py                          # the canonical trace
    python scripts/render_trace.py --trace logs/trace_x.jsonl --out demo/x.gif
    python scripts/render_trace.py --scale 8 --hold 18      # bigger, slower
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from compiler_adapter.adapter import states_from_trace  # noqa: E402

DEFAULT_TRACE = REPO_ROOT / "demo" / "trace_canonical.jsonl"
DEFAULT_OUT = REPO_ROOT / "demo" / "pixel_swarm_demo.gif"


def load_trace(path):
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"FAIL: no such trace: {path}")
    records = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"FAIL: {path}:{i} is not valid JSON: {exc}") from exc
    if not records:
        raise SystemExit(f"FAIL: {path} contains no records")
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", default=str(DEFAULT_TRACE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--scale", type=int, default=6, help="logical pixel -> screen pixels")
    parser.add_argument("--hold", type=int, default=14, help="frames held per tick")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    records = load_trace(args.trace)
    adapter = states_from_trace(records)

    if not args.quiet:
        print(f"trace : {args.trace} ({len(records)} ticks)")
        print(f"out   : {args.out}")
        print("-" * 62)
        for state, record in zip(adapter.states, records):
            mark = "  <-OVERRIDE" if state["badge"] else ""
            line = f'  "{state["line"]}"' if state["line"] else ""
            print(f"  tick {record.get('tick'):>2}  {state['mood']:<8} "
                  f"{state['action']:<12}{line}{mark}")
        print("-" * 62)

    written = adapter.write_gif(
        args.out, scale=args.scale, frames_per_state=args.hold, fps=args.fps
    )
    size_kb = Path(args.out).stat().st_size / 1024

    if not args.quiet:
        seconds = written / args.fps
        print(f"{written} frames, {seconds:.1f}s at {args.fps}fps, {size_kb:.0f} KB")
        overrides = [r.get("tick") for r, s in zip(records, adapter.states) if s["badge"]]
        print(f"override tick(s): {overrides if overrides else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
