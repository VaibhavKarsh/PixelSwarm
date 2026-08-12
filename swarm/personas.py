"""One function per persona (Architecture doc Section 3).

PHASE 3: each persona now has BOTH a mock and a real model-backed implementation,
and the four public functions dispatch between them per persona. Which ones are
real is driven by `config["real_personas"]`, so the Roadmap's required workflow -
"run with mood-picker real and the other three still mocked", then add the next -
is a config change rather than an edit:

    python -m swarm.harness --real mood
    python -m swarm.harness --real mood,action
    python -m swarm.harness --real all

Default is all-mocked, which keeps the test suite fast and deterministic.

The real implementations send the Section 3.5 prompts VERBATIM. The Roadmap is
explicit that prompt wording is frozen during Phase 3 and that tuning belongs to
Phase 6; scripts/validate_prompt_fidelity.py enforces that mechanically, and the
prompts live in one module-level table below so there is a single place for the
checker to compare against.

The mocks stay because Phase 3 depends on running with only some personas real,
and because every non-model test in the suite uses them.

The mocks are deterministic heuristics rather than fixed constants, for two
reasons the Roadmap cares about:
  - a run that always returns the same mood would exercise none of the loop's
    interesting paths, and Phase 2's Definition of Done wants a trace log worth
    reading;
  - the Demo Script's t=60 conflict (mood turns alert while the action-picker is
    "still biased toward celebrate from momentum") has to be representable, or
    Phase 4 would discover only at the end that the trace format cannot express
    its headline beat.

They stay deliberately dumb: no lookahead, no memory beyond the state object,
and no attempt to be a rules engine. Each returns exactly the output schema of
its Section 3 subsection, so the harness code written against these keeps
working unchanged when real models arrive.
"""

import json

from swarm.state import banned_actions

# --- Section 3.5 prompts, VERBATIM -------------------------------------------
# Do not edit these to "improve" behaviour: the Roadmap freezes prompt wording
# for Phase 3 and reserves tuning for Phase 6, and
# scripts/validate_prompt_fidelity.py fails the build if these drift from
# Architecture doc Section 3.5 by even one character.

MOOD_PROMPT = """You are the mood-picker for a pixel-art character. Given the character's
current state and recent events, decide the character's new mood.

Allowed moods (choose exactly one): idle, happy, excited, alert, sad, angry

Weigh recent events by urgency, not just by recency or count: threat or danger
signals take precedence over social or hype signals when both are present.

Threat signals are ordered among themselves. An all-clear that arrives after a
danger supersedes it: if the most recent threat-related signal says the danger
has passed, treat the threat as over and let the mood recover, however many
earlier dangers are still in view.

Use idle when nothing is happening: quiet, a timeout, or calm chat with no other
recent event. Calm is an absence of stimulus, not a positive event. But a hype
or positive event IS something happening, so never read one as idle.

Scale positive moods by intensity: a mild positive event is happy, a strong one
(intensity 0.8 or above) is excited. As events recede and nothing new arrives,
settle back toward idle.

An active, unresolved threat is alert. Reserve sad and angry for an actual
negative outcome affecting the character - a loss, a setback, a provocation -
not for the mere presence or absence of a threat. If no recent event describes
such an outcome, do not use them.

Respond with ONLY valid JSON, no other text:
{"mood": "<one of the allowed moods>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}"""

ACTION_PROMPT = """You are the action-picker for a pixel-art character. Given the character's
current state, recent events, and the mood that was just decided, choose
what the character should be doing right now.

Allowed actions (choose exactly one): idle_loop, wave, jump, duck, celebrate, look_around

Typical mood-action pairings (a guide, not a hard rule):
idle -> idle_loop | happy -> wave | excited -> celebrate or jump
alert -> look_around or duck | sad -> idle_loop | angry -> idle_loop

Respond with ONLY valid JSON, no other text:
{"action": "<one of the allowed actions>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}"""

LINE_PROMPT = """You are the dialogue-line picker for a pixel-art character. Given the
character's mood and action just decided, optionally propose a very short
spoken line (under 8 words). Most ticks should have NO line — only propose
one if mood or action just changed meaningfully.

Respond with ONLY valid JSON, no other text:
{"line": "<short line or null>", "reason": "<short reason, max 15 words>"}"""

# The checker prompt carries the live transitions table, so it is built per run.
CHECKER_PROMPT_TEMPLATE = """You are the transition-checker for a pixel-art character animation system.
You receive the character's current committed state and three proposals
(mood, action, line) from other decision-makers. Check the proposals
against the disallowed-transition rules below. If all proposals are valid,
approve them as-is. If not, reject and supply a valid fallback.

Allowed moods (final_mood must be exactly one of these):
idle, happy, excited, alert, sad, angry

Allowed actions (final_action must be exactly one of these):
idle_loop, wave, jump, duck, celebrate, look_around

Disallowed transitions:
{transitions}

There are two kinds of rule and BOTH must hold.
by_mood[final_mood] lists actions not allowed while in that mood.
by_previous_action[current_action] lists actions that may not come directly
after the action the character is performing right now - those need an
intermediate step first.

The table is exhaustive: it lists every restriction that exists. A mood with no
by_mood entry restricts nothing, and an action with no by_previous_action entry
may be followed by anything. Approve unless a rule above literally names the
proposed action - never infer a restriction because one seems plausible for the
mood.

If you reject, you MUST pick a final_action that breaks neither rule. Never
return the action you just rejected. Do not fall back to current_action if it
is disallowed. If no better choice is obvious, use idle_loop.

Respond with ONLY valid JSON, no other text:
{{"verdict": "approve" or "reject", "final_mood": "...", "final_action": "...",
 "final_line": "... or null", "reason": "<short reason, max 20 words>"}}"""


def strip_doc_keys(value):
    """Drop `_`-prefixed documentation keys, recursively.

    config/*.json uses a leading underscore for human-facing commentary, and
    `scripts/validate_configs.py` already ignores those keys. They must not reach
    a prompt: they would spend tokens on notes written for maintainers, and one
    of them literally discusses the *action-picker*, which is exactly the kind of
    stray text that confuses a small model reading a rules table.
    """
    if isinstance(value, dict):
        return {k: strip_doc_keys(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_doc_keys(v) for v in value]
    return value


def checker_prompt(transitions):
    """Section 3.5's checker prompt with the live rules table injected."""
    rules = strip_doc_keys(transitions)
    return CHECKER_PROMPT_TEMPLATE.format(transitions=json.dumps(rules, indent=2))


PROMPTS = {
    "mood": MOOD_PROMPT,
    "action": ACTION_PROMPT,
    "line": LINE_PROMPT,
    # "checker" is built per call by checker_prompt(); listed here for discovery.
}


# Section 3.5's "typical mood-action pairings" guide, encoded for the mock.
# Real models are given this as prose; the mock reads it as a table.
MOOD_FOR_EVENT = {
    "chat_hype_spike": "excited",
    "chat_calm": "idle",
    "game_danger": "alert",
    "game_safe": "happy",
    "idle_timeout": "idle",
}

ACTION_FOR_MOOD = {
    "idle": "idle_loop",
    "happy": "wave",
    "excited": "celebrate",
    "alert": "look_around",
    "sad": "idle_loop",
    "angry": "idle_loop",
}


def _latest_event(state):
    return state.recent_events[-1] if state.recent_events else None


# --- dispatch: real vs mock, per persona -------------------------------------

def _is_real(name, config):
    """True if `name` should use a real model call this run."""
    selected = (config or {}).get("real_personas") or []
    return name in set(selected)


def _model_for(name, config):
    return ((config or {}).get("models") or {}).get(name, "")


def _client_for(config):
    """The injected client if a test supplied one, else the real module.

    Both expose chat()/call_json(), so nothing downstream cares which it got.
    """
    injected = (config or {}).get("_client")
    if injected is not None:
        return injected
    from swarm import model_client
    return model_client


def mood_picker(state, config=None, **kw):
    """Section 3.1 -> {"mood", "confidence", "reason"}."""
    if _is_real("mood", config):
        return _real_mood_picker(state, config, **kw)
    return _mock_mood_picker(state, config, **kw)


def action_picker(state, proposed_mood, config=None, **kw):
    """Section 3.2 -> {"action", "confidence", "reason"}."""
    if _is_real("action", config):
        return _real_action_picker(state, proposed_mood, config, **kw)
    return _mock_action_picker(state, proposed_mood, config, **kw)


def dialogue_line(state, proposed_mood, proposed_action, config=None, **kw):
    """Section 3.3 -> {"line", "reason"}."""
    if _is_real("line", config):
        return _real_dialogue_line(state, proposed_mood, proposed_action, config, **kw)
    return _mock_dialogue_line(state, proposed_mood, proposed_action, config, **kw)


def transition_checker(state, proposals, transitions, config=None, **kw):
    """Section 3.4 -> {"verdict", "final_mood", "final_action", "final_line", "reason"}."""
    if _is_real("checker", config):
        return _real_transition_checker(state, proposals, transitions, config, **kw)
    return _mock_transition_checker(state, proposals, transitions, config, **kw)


# --- real implementations (Phase 3) ------------------------------------------

def _real_mood_picker(state, config, **_):
    return _client_for(config).call_json(
        _model_for("mood", config),
        PROMPTS["mood"],
        {"state": state.to_prompt_dict()},
        runtime=(config or {}).get("runtime", {}),
    )


def _real_action_picker(state, proposed_mood, config, **_):
    return _client_for(config).call_json(
        _model_for("action", config),
        PROMPTS["action"],
        {"state": state.to_prompt_dict(), "proposed_mood": proposed_mood},
        runtime=(config or {}).get("runtime", {}),
    )


def _real_dialogue_line(state, proposed_mood, proposed_action, config, **_):
    return _client_for(config).call_json(
        _model_for("line", config),
        PROMPTS["line"],
        {
            "state": state.to_prompt_dict(),
            "proposed_mood": proposed_mood,
            "proposed_action": proposed_action,
        },
        runtime=(config or {}).get("runtime", {}),
    )


def _real_transition_checker(state, proposals, transitions, config, **_):
    return _client_for(config).call_json(
        _model_for("checker", config),
        checker_prompt(transitions),
        {
            "state": state.to_prompt_dict(),
            "proposed_mood": proposals["mood"],
            "proposed_action": proposals["action"],
            "proposed_line": proposals["line"],
        },
        runtime=(config or {}).get("runtime", {}),
    )


# --- mock implementations (Phase 2, retained) --------------------------------

def _mock_mood_picker(state, config=None, **_):
    event = _latest_event(state)
    if event is None:
        return {
            "mood": state.current_mood,
            "confidence": 0.5,
            "reason": "no recent events; holding current mood",
        }

    mood = MOOD_FOR_EVENT.get(event.type, state.current_mood)
    return {
        "mood": mood,
        "confidence": round(min(1.0, 0.5 + event.intensity / 2), 2),
        "reason": f"latest event {event.type} at intensity {event.intensity}",
    }


def _mock_action_picker(state, proposed_mood, config=None, **_):
    """Section 3.2 -> {"action", "confidence", "reason"}.

    Simply follows the proposed mood via Section 3.5's pairing guide, which is
    what the real action-picker was measured doing (Section 7.1d).

    This used to carry a "momentum" special case - keep proposing `celebrate`
    while the mood turns `alert` - to manufacture the t=60 conflict. That was
    removed on 2026-07-30 once `by_previous_action` made the conflict arise from
    config instead. Keeping it would have made the mock behave *unlike* the real
    model at the single most important tick, which defeats the purpose of having
    a mock at all. Section 3.2 remains explicit that this persona does not
    enforce validity - that is the checker's job.
    """
    mood = proposed_mood.get("mood", state.current_mood)
    action = ACTION_FOR_MOOD.get(mood, "idle_loop")
    return {
        "action": action,
        "confidence": round(proposed_mood.get("confidence", 0.7), 2),
        "reason": f"typical pairing for mood {mood}",
    }


def _mock_dialogue_line(state, proposed_mood, proposed_action, config=None, **_):
    """Section 3.3 -> {"line", "reason"}.

    Biased hard toward silence, as Section 3.3 requires: a line is proposed only
    when the mood actually changes, never on a steady-state tick.
    """
    mood = proposed_mood.get("mood", state.current_mood)
    if mood == state.current_mood:
        return {"line": None, "reason": "mood unchanged; staying quiet"}

    lines = {
        "excited": "Let's go!!",
        "happy": "Hey there!",
        "alert": "What was that?",
        "sad": "Oh no...",
        "angry": "Not again!",
        "idle": None,
    }
    line = lines.get(mood)
    if line is None:
        return {"line": None, "reason": f"mood {mood} does not warrant a line"}
    return {"line": line, "reason": f"mood just changed to {mood}"}


def _mock_transition_checker(state, proposals, transitions, config=None, **_):
    """Section 3.4 -> {"verdict", "final_mood", "final_action", "final_line", "reason"}.

    Applies the Section 4 table the way the revised Section 3.5 prompt instructs
    a real model to: on reject it must supply a fallback that is legal for
    `final_mood`, and must never echo back the rejected action or fall back to
    `current_action` when that is itself disallowed. The Phase 0 benchmark showed
    every real model getting this wrong before the prompt was fixed, so the mock
    deliberately encodes the corrected behaviour rather than the buggy one.
    """
    mood = proposals["mood"].get("mood", state.current_mood)
    action = proposals["action"].get("action", state.current_action)
    line = proposals["line"].get("line")

    # Both Section 4 rule kinds, smoothness measured against the pose the
    # character is animating right now.
    banned = banned_actions(transitions, mood, state.current_action)

    if action not in banned:
        return {
            "verdict": "approve",
            "final_mood": mood,
            "final_action": action,
            "final_line": line,
            "reason": f"{action} is permitted for mood {mood}",
        }

    # Name the rule that actually fired. Attributing a smoothness rejection to
    # the pairing rule would make the trace lie about why the character did what
    # it did, which is the one thing the trace exists to prevent (PRD #5).
    by_mood_banned = set(
        (transitions.get("by_mood", {}).get(mood) or {}).get("disallowed_next_action") or []
    )
    if action in by_mood_banned:
        cause = f"disallowed for mood {mood}"
    else:
        cause = f"cannot follow {state.current_action} directly; needs an intermediate step"

    fallback = _legal_fallback(mood, banned, config)
    return {
        "verdict": "reject",
        "final_mood": mood,
        "final_action": fallback,
        "final_line": line,
        "reason": f"{action} {cause}; substituted {fallback}",
    }


def _legal_fallback(mood, banned, config):
    """Pick an action that is legal for `mood`.

    Prefers the configured fallback (`idle_loop`, which no rule in Section 4
    disallows), then the mood's typical pairing, then anything permitted.
    """
    preferred = (config or {}).get("fallback", {}).get("action", "idle_loop")
    for candidate in (preferred, ACTION_FOR_MOOD.get(mood), "idle_loop"):
        if candidate and candidate not in banned:
            return candidate
    for candidate in (config or {}).get("actions", []):
        if candidate not in banned:
            return candidate
    # Unreachable given validate_configs.py rejects a mood that bans every
    # action, but never return a disallowed action just because the search ran out.
    return "idle_loop"
