"""Minimal pixel-art renderer standing in for the Pixel-World Compiler.

Deliberately knows nothing about the swarm: it accepts named moods and actions
and draws frames. See `docs/02_ARCHITECTURE_HARNESS_SPEC.md` Section 8 for why this is
a separate package rather than part of compiler_adapter/.
"""

from pixel_world.renderer import (  # noqa: F401
    ACTIONS,
    MOODS,
    Renderer,
    render_states,
)
