"""Canned stand-in for swarm/model_client.py (docs/06_TESTING_STRATEGY.md Section 2.1).

06 calls this "the single most important piece of test infrastructure in the
project", and the reason is narrow: without it every persona test needs a live
Ollama, which makes the suite slow, flaky, and non-repeatable. With it, the
awkward cases - a truncated response, an invented enum value, a hang - are
ordinary deterministic tests.

It exposes the same surface the real client does (`chat`, `call_json`, plus the
exception types), so code under test cannot tell the difference.

    client = FakeModelClient()
    client.queue("mood", VALID["mood"]["excited"])
    client.queue("mood", MALFORMED["truncated"])
    text = client.chat("any-model", MOOD_SYSTEM_PROMPT, {...})
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from swarm.model_client import (  # noqa: E402  (re-exported for tests)
    ModelError,
    ModelParseFailure,
    ModelTimeout,
    ModelUnavailable,
    extract_json,
)

MOODS = ["idle", "happy", "excited", "alert", "sad", "angry"]
ACTIONS = ["idle_loop", "wave", "jump", "duck", "celebrate", "look_around"]


def _mood(mood, confidence=0.8, reason="canned response"):
    return json.dumps({"mood": mood, "confidence": confidence, "reason": reason})


def _action(action, confidence=0.8, reason="canned response"):
    return json.dumps({"action": action, "confidence": confidence, "reason": reason})


def _line(line, reason="canned response"):
    return json.dumps({"line": line, "reason": reason})


def _check(verdict, mood, action, line=None, reason="canned response"):
    return json.dumps({
        "verdict": verdict, "final_mood": mood, "final_action": action,
        "final_line": line, "reason": reason,
    })


# A valid canned response for EVERY enum value, so a test can drive the harness
# to any state without needing a real model to happen to produce it (06 §2.1).
VALID = {
    "mood": {m: _mood(m) for m in MOODS},
    "action": {a: _action(a) for a in ACTIONS},
    "line": {"silent": _line(None), "speaking": _line("Let's go!!")},
    "check": {
        "approve": _check("approve", "excited", "celebrate"),
        "reject": _check("reject", "alert", "idle_loop", reason="celebrate disallowed for alert"),
    },
}

# The malformed battery 06 §2.1 enumerates by name. Every entry is something a
# small local model has actually been observed doing.
MALFORMED = {
    "empty": "",
    "whitespace": "   \n  ",
    "prose_only": "Sure! The character should probably look around now.",
    "truncated": '{"mood": "excited", "confidence": 0.8',
    "fenced": '```json\n{"mood": "happy", "confidence": 0.7, "reason": "fenced"}\n```',
    "prose_wrapped": 'Here you go:\n{"mood": "sad", "confidence": 0.6, "reason": "wrapped"}\nHope that helps!',
    "out_of_enum": _mood("neutral"),
    "out_of_enum_action": _action("moonwalk"),
    "missing_key": json.dumps({"confidence": 0.9, "reason": "no mood key"}),
    "wrong_type": json.dumps({"mood": 42, "confidence": 0.9, "reason": "numeric mood"}),
    "array_not_object": '[{"mood": "idle"}]',
    "scalar_not_object": '"excited"',
    "null": "null",
    "invented_action": _check("reject", "alert", "defensive_stance"),
    "illegal_fallback": _check("reject", "alert", "jump"),
    "echoes_forbidden": _check("reject", "alert", "celebrate"),
    "bad_verdict": _check("maybe", "alert", "idle_loop"),
    "empty_reason": json.dumps({"mood": "idle", "confidence": 0.5, "reason": ""}),
    # Measured on qwen3.5:9b: a backslash before quotes that are not inside a
    # string. 10 of 16 persona failures in the Phase 4 baseline. format="json"
    # does not prevent it.
    "over_escaped": '{"line": null, "reason": \\"no line needed yet.\\"}',
    "over_escaped_mood": '{"mood": \\"idle\\", "confidence": 0.5, "reason": \\"quiet\\"}',
}

# Which entries SHOULD survive extract_json (they contain a real JSON object)
# versus which must raise ModelParseFailure. Keeps tests honest about the
# difference between "unparseable" and "parsed but semantically wrong".
PARSEABLE = {
    "truncated": False, "empty": False, "whitespace": False, "prose_only": False,
    "array_not_object": False, "scalar_not_object": False, "null": False,
    "fenced": True, "prose_wrapped": True, "out_of_enum": True,
    "out_of_enum_action": True, "missing_key": True, "wrong_type": True,
    "invented_action": True, "illegal_fallback": True, "echoes_forbidden": True,
    "bad_verdict": True, "empty_reason": True,
    # Recoverable by the Section 3.6 permissive extractor's repair pass.
    "over_escaped": True, "over_escaped_mood": True,
}


class _Hang(Exception):
    """Marker used internally to represent a simulated stall."""


class FakeModelClient:
    """Drop-in replacement for swarm.model_client with scripted responses.

    Responses are queued per persona key ("mood", "action", "line", "check").
    A persona with an empty queue falls back to `default_for`, so a test only has
    to script the persona it cares about.
    """

    def __init__(self, default_mood="idle", default_action="idle_loop"):
        self.queues = {"mood": [], "action": [], "line": [], "check": []}
        self.calls = []          # (persona, model) in call order
        self.raise_next = {}     # persona -> exception instance to raise once
        self.default_mood = default_mood
        self.default_action = default_action

    # -- scripting --

    def queue(self, persona, *responses):
        """Queue one or more raw response strings for `persona`."""
        self.queues[persona].extend(responses)
        return self

    def queue_exception(self, persona, exc):
        """Make the next call for `persona` raise (timeout, unavailable, ...)."""
        self.raise_next[persona] = exc
        return self

    def queue_timeout(self, persona):
        return self.queue_exception(persona, ModelTimeout("simulated stall"))

    def queue_unavailable(self, persona):
        return self.queue_exception(persona, ModelUnavailable("simulated server down"))

    # -- the client surface --

    def default_for(self, persona):
        return {
            "mood": VALID["mood"][self.default_mood],
            "action": VALID["action"][self.default_action],
            "line": VALID["line"]["silent"],
            "check": _check("approve", self.default_mood, self.default_action),
        }[persona]

    def chat(self, model, system_prompt, user_payload, runtime=None, client=None):
        persona = persona_of(system_prompt)
        self.calls.append((persona, model))

        if persona in self.raise_next:
            raise self.raise_next.pop(persona)

        queue = self.queues.get(persona) or []
        return queue.pop(0) if queue else self.default_for(persona)

    def call_json(self, model, system_prompt, user_payload, runtime=None, client=None):
        return extract_json(self.chat(model, system_prompt, user_payload, runtime, client))

    # -- introspection --

    def call_count(self, persona=None):
        if persona is None:
            return len(self.calls)
        return sum(1 for p, _ in self.calls if p == persona)


def persona_of(system_prompt):
    """Identify which persona a system prompt belongs to.

    Matches on the role sentence each Section 3.5 prompt opens with, so the fake
    client needs no extra plumbing through the call.
    """
    # Match on the ROLE SENTENCE only, not the whole prompt. The checker prompt
    # embeds the rules table, whose text can mention other persona names; keying
    # off the opening declaration avoids that whole class of false match.
    text = (system_prompt or "").lower()
    opening = text.split("\n\n", 1)[0]
    for needle, persona in (
        ("transition-checker", "check"),
        ("mood-picker", "mood"),
        ("action-picker", "action"),
        ("dialogue-line", "line"),
    ):
        if needle in opening:
            return persona
    raise ValueError(f"cannot identify persona from prompt opening: {opening[:80]!r}")


__all__ = [
    "FakeModelClient", "VALID", "MALFORMED", "PARSEABLE", "persona_of",
    "ModelError", "ModelParseFailure", "ModelTimeout", "ModelUnavailable",
    "MOODS", "ACTIONS",
]
