"""Config fingerprinting for trace records (Architecture doc Section 6).

Every published number in this repo is "measured under some configuration", and
until now the trace files did not say which. Two traces in `logs/` were
indistinguishable even when a prompt clause had been added between them - a
mistake that actually happened during Phase 6, where a stale trace was compared
against a fresh one and produced the opposite conclusion.

`config_version` fixes that: a short hash stamped on every tick record, plus a
`<trace>.meta.json` sidecar that says what the hash expands to.

**Prompt text is part of the fingerprint, not just the JSON configs.** That is
the whole point. The configs change rarely; the prompts are what moved the
reliability figure from 65% to 85%, and a fingerprint that ignored them would
call two materially different runs identical. Hashing the prompts is what makes
this a drift detector rather than a decoration.

Deliberately NOT in the fingerprint: the event scenario. That is a run input,
not configuration - the same config is meant to be run against several scenarios
- so it is recorded in the sidecar under `scenario` instead.

`_`-prefixed documentation keys are stripped before hashing (via the same
`strip_doc_keys` the prompts use), so editing a maintainer comment in
`config/*.json` does not invalidate a measurement.
"""

import hashlib
import json

from swarm.personas import (
    ACTION_PROMPT,
    CHECKER_PROMPT_TEMPLATE,
    LINE_PROMPT,
    MOOD_PROMPT,
    strip_doc_keys,
)

# Bump only if the fingerprint recipe itself changes (e.g. a new input starts
# being hashed). Without this, an old and a new recipe could collide in meaning:
# the same 12 characters would stand for two different things.
FINGERPRINT_RECIPE = 1

SHORT_LEN = 12

PROMPTS = {
    "mood": MOOD_PROMPT,
    "action": ACTION_PROMPT,
    "line": LINE_PROMPT,
    # The checker template, not the rendered prompt: the table it interpolates
    # is config/transitions.json, which is hashed separately below.
    "check": CHECKER_PROMPT_TEMPLATE,
}


def _canonical(obj) -> str:
    """Serialise so that key order and whitespace cannot change the hash."""
    return json.dumps(strip_doc_keys(obj), sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hashes() -> dict:
    """Per-persona prompt digests, so a diff says WHICH prompt moved."""
    return {name: _digest(text)[:SHORT_LEN] for name, text in PROMPTS.items()}


def config_fingerprint(config, transitions) -> str:
    """The short hash stamped on every trace record."""
    payload = _canonical({
        "recipe": FINGERPRINT_RECIPE,
        "personas": config,
        "transitions": transitions,
        "prompts": {name: _digest(text) for name, text in PROMPTS.items()},
    })
    return _digest(payload)[:SHORT_LEN]


def fingerprint_details(config, transitions, scenario=None, mode=None) -> dict:
    """What the short hash expands to - written beside the trace as a sidecar.

    `scenario` and `mode` are run inputs rather than config; they are recorded
    here so a trace is self-describing, but they are not part of the hash.
    """
    return {
        "config_version": config_fingerprint(config, transitions),
        "recipe": FINGERPRINT_RECIPE,
        "scenario": scenario,
        "mode": mode,
        "models": dict(sorted(config.get("models", {}).items())),
        "prompt_hashes": prompt_hashes(),
        "personas_config": strip_doc_keys(config),
        "transitions_config": strip_doc_keys(transitions),
    }
