"""Shared state object passed to every persona call (Architecture doc Section 2).

This is the *only* view of the world any persona gets - no persona talks to the
compiler, and none sees more history than the rolling window allows.

Phase 2. Contains no model calls and no decision logic; it only holds state and
enforces the window bounds from Section 2.
"""

from dataclasses import dataclass, field, replace
from typing import Optional

# Section 2, decided 2026-07-30. Overridable from config/personas.json so the
# values are tunable without touching code.
DEFAULT_MAX_EVENTS = 5
DEFAULT_MAX_AGE_S = 60.0

DEFAULT_MOOD = "idle"
DEFAULT_ACTION = "idle_loop"


@dataclass(frozen=True)
class Event:
    """One inbound event (Interface Contract doc Section 2.1)."""

    type: str
    intensity: float
    ts: float
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw):
        return cls(
            type=raw["type"],
            intensity=float(raw["intensity"]),
            ts=float(raw["ts"]),
            meta=dict(raw.get("meta") or {}),
        )

    def to_prompt_dict(self):
        """Section 2 shows events in the state object as type/intensity/ts only.

        `meta` is deliberately dropped: it is routing information for the
        harness, not something a persona should reason about, and leaving it out
        keeps the prompt small (Section 2's stated intent).
        """
        return {"type": self.type, "intensity": self.intensity, "ts": self.ts}


@dataclass
class SwarmState:
    """The committed state of the character, plus its recent-event window."""

    current_mood: str = DEFAULT_MOOD
    current_action: str = DEFAULT_ACTION
    last_line: Optional[str] = None
    ticks_since_last_change: int = 0
    recent_events: list = field(default_factory=list)

    max_events: int = DEFAULT_MAX_EVENTS
    max_age_s: float = DEFAULT_MAX_AGE_S

    # --- window management ---------------------------------------------------

    def add_event(self, event, now_ts=None):
        """Append an event and re-apply the window bounds."""
        self.recent_events.append(event)
        self.prune(now_ts if now_ts is not None else event.ts)

    def prune(self, now_ts):
        """Apply BOTH Section 2 bounds: at most `max_events`, none older than
        `max_age_s`. Whichever binds first wins."""
        if self.max_age_s is not None:
            cutoff = now_ts - self.max_age_s
            # `>=` keeps an event exactly on the boundary: at now_ts=75 with a
            # 60s window, the t=15 event is exactly 60s old and still counts.
            self.recent_events = [e for e in self.recent_events if e.ts >= cutoff]
        if self.max_events is not None and len(self.recent_events) > self.max_events:
            self.recent_events = self.recent_events[-self.max_events:]

    # --- committing a decision -----------------------------------------------

    def commit(self, mood, action, line):
        """Adopt a decided (mood, action, line) and update the change counter.

        `ticks_since_last_change` counts ticks since mood or action last moved.
        A dialogue line alone does not reset it - lines are transient by design
        (Section 3.3 biases toward silence), so counting them as "change" would
        keep the counter pinned near zero and make it useless as a signal.
        """
        changed = (mood != self.current_mood) or (action != self.current_action)
        self.current_mood = mood
        self.current_action = action
        self.last_line = line
        self.ticks_since_last_change = 0 if changed else self.ticks_since_last_change + 1
        return changed

    # --- views ---------------------------------------------------------------

    def to_prompt_dict(self):
        """Exactly the Section 2 shape. This is what personas receive."""
        return {
            "current_mood": self.current_mood,
            "current_action": self.current_action,
            "last_line": self.last_line,
            "ticks_since_last_change": self.ticks_since_last_change,
            "recent_events": [e.to_prompt_dict() for e in self.recent_events],
        }

    def snapshot(self):
        """An independent copy, so a tick's logged input state is not mutated by
        the rest of that tick."""
        return replace(self, recent_events=list(self.recent_events))


# --- transition rules (Architecture doc Section 4) ---------------------------
# One implementation, used by the harness invariant, the mock checker and the
# config validator. Three copies of this logic would drift, and a rule that is
# enforced in one place and not another is worse than no rule.

def banned_actions(transitions, final_mood, current_action):
    """Actions that may not be committed, given both rule kinds.

    by_mood[final_mood]            - pairing: not allowed while in this mood.
    by_previous_action[current_action] - smoothness: may not directly follow the
                                     pose the character is animating right now.
    """
    by_mood = (transitions.get("by_mood") or {}).get(final_mood) or {}
    by_prev = (transitions.get("by_previous_action") or {}).get(current_action) or {}
    return set(by_mood.get("disallowed_next_action") or []) | set(
        by_prev.get("disallowed_next_action") or []
    )


def is_legal(transitions, final_mood, current_action, action):
    return action not in banned_actions(transitions, final_mood, current_action)


def legal_actions(transitions, final_mood, current_action, actions):
    banned = banned_actions(transitions, final_mood, current_action)
    return [a for a in actions if a not in banned]


def load_events(raw_events):
    """Build the event list from demo_sequence.json.

    Sorted by `ts` with a STABLE sort, so events sharing a timestamp keep their
    file order (Interface Contract doc Section 2.1, decided 2026-07-30).
    """
    events = [Event.from_dict(r) for r in raw_events]
    return sorted(events, key=lambda e: e.ts)


def state_from_config(config):
    """Build the initial state, honouring the window bounds in personas.json."""
    return SwarmState(
        max_events=config.get("recent_events_max_count", DEFAULT_MAX_EVENTS),
        max_age_s=float(config.get("recent_events_max_age_s", DEFAULT_MAX_AGE_S)),
    )
