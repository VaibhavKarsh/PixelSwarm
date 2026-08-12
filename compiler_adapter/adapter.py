"""Translates a Directive into renderer calls (Interface Contract doc Section 3.3).

Phase 5. This is the ONLY module that knows about both sides: it imports the
Directive shape from the harness side and `pixel_world` from the renderer side.
`pixel_world/` imports nothing from `swarm/` or from here, which is what keeps the
two-system separation in the Interface Contract real rather than nominal - and it
is enforced by a test, not by this comment.

Section 3.3 is explicit that the adapter carries NO decision-making: every choice
was already made upstream by the swarm. So the rules here are deliberately dull -
map a name to a name, and refuse anything that is not a name we know.

Interface Contract Section 3.2's four [TODO] questions are answered here for the
stand-in renderer (see Architecture doc Section 8 for why there is a stand-in):

  Q. What selects the frame-set to render?
     A. pixel_world.Renderer.render(mood, action, ...), keyed by NAMED states -
        no numeric IDs or file paths.
  Q. One-off actions vs sustained loops?
     A. Every action is a short cycle the renderer holds for as long as it is
        asked to. The harness never needs to distinguish the two, so Pixel
        Swarm's assumption that both are possible holds trivially.
  Q. Does it render dialogue?
     A. Yes - `line` becomes a speech bubble. No new core work was needed.
  Q. Hold-until-next-directive, or explicit signalling?
     A. Hold. A Directive stays active until another replaces it, matching
        Interface Contract Section 4, so no "keep going" signal is required.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pixel_world import ACTIONS, MOODS, Renderer, render_states  # noqa: E402


class DirectiveError(ValueError):
    """A Directive the renderer could not be asked to draw."""


def validate_directive(directive):
    """Reject anything the renderer could not draw.

    **Resolves open ambiguity #2** (docs/06_TESTING_STRATEGY.md Section 4): the adapter
    REJECTS AND RAISES on an out-of-spec Directive rather than passing it through.

    "No decision-making" (Section 3.3) does not mean "no validation". Passing an
    unknown state down would make the renderer silently hold its previous frame,
    which looks identical on screen to a tick that legitimately changed nothing -
    exactly the silent-failure mode the trace log exists to prevent. Failing loudly
    at the boundary keeps a rendering bug distinguishable from a swarm decision.
    """
    if not isinstance(directive, dict):
        raise DirectiveError(f"directive must be an object, got {type(directive).__name__}")

    for key in ("mood", "action"):
        if key not in directive:
            raise DirectiveError(f"directive is missing required key {key!r}")

    mood, action = directive["mood"], directive["action"]
    if mood not in MOODS:
        raise DirectiveError(f"unknown mood {mood!r}; renderer knows {MOODS}")
    if action not in ACTIONS:
        raise DirectiveError(f"unknown action {action!r}; renderer knows {ACTIONS}")

    line = directive.get("line")
    if line is not None and not isinstance(line, str):
        raise DirectiveError(f"line must be a string or null, got {type(line).__name__}")
    return directive


def directive_to_state(directive, caption=None, badge=None):
    """Directive -> the renderer's named-state dict. A pure rename, no logic."""
    validate_directive(directive)
    return {
        "mood": directive["mood"],
        "action": directive["action"],
        "line": directive.get("line") or None,
        "caption": caption,
        "badge": badge,
    }


class RenderAdapter:
    """Receives Directives and draws them.

    Usable as a live sink from the harness (`Harness(..., sink=adapter.send)`) or
    fed a whole trace after the fact. Holds the last Directive, per Interface
    Contract Section 4: a Directive stays active until another replaces it.
    """

    def __init__(self, renderer=None):
        self.renderer = renderer or Renderer()
        self.states = []
        self.last_directive = None

    def send(self, directive, caption=None, badge=None):
        """Accept one Directive. Returns the state it was translated into."""
        state = directive_to_state(directive, caption=caption, badge=badge)
        self.states.append(state)
        self.last_directive = directive
        return state

    def frame(self, frame=0):
        """Render the currently-held Directive. None if nothing has arrived yet."""
        if not self.states:
            return None
        state = self.states[-1]
        return self.renderer.render(
            mood=state["mood"], action=state["action"], line=state["line"],
            caption=state["caption"], badge=state["badge"], frame=frame,
        )

    def write_gif(self, out_path, **kwargs):
        """Render everything received so far to an animated GIF."""
        if not self.states:
            raise DirectiveError("no directives received; nothing to render")
        return render_states(self.states, out_path, **kwargs)


def states_from_trace(records):
    """Build renderer states from trace records (Architecture doc Section 6).

    Reads only `final_state`, `trigger` and `verdict` - the committed decision and
    enough context to caption it. The proposals and per-persona reasoning stay on
    the harness side of the boundary, because the renderer has no business
    knowing the swarm deliberated at all.
    """
    adapter = RenderAdapter()
    for record in records:
        final = record["final_state"]
        trigger = record.get("trigger", {})
        event = trigger.get("event_type") or "timer"
        verdict = (record.get("verdict") or {}).get("verdict")
        adapter.send(
            {
                "tick": record.get("tick"),
                "mood": final["current_mood"],
                "action": final["current_action"],
                "line": final.get("last_line"),
                "ts": trigger.get("ts"),
            },
            caption=f"tick {record.get('tick')}  ·  {event}",
            badge="OVERRIDE" if verdict == "reject" else None,
        )
    return adapter
