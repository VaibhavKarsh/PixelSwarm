"""Thin wrapper around the local inference runtime (Architecture doc Section 9).

Phase 3. One job: take a system prompt plus an input payload and return the raw
text the model produced. It makes no decisions, knows nothing about personas, and
does no schema validation - the harness owns that (Section 5).

Every non-obvious setting here was paid for by a Phase 0 measurement, so none of
them are style preferences:

  think=False   Several installed models are reasoning models. Architecture doc
                Section 7.1 measured a single trivial call at 76-159s emitting
                up to 13,200 characters of hidden reasoning; the same call with
                think=False takes ~1.3s. Roughly 100x. Without this the project
                does not work.
  timeout       ollama.Client defaults to NO timeout, so a stalled call hangs
                the run forever. Always pass one explicitly.
  format=json   Section 7.2's resolution of Section 3.6. It guarantees parseable
                JSON at best - never correct keys or in-enum values - which is
                why extract_json below is still a second layer and the harness
                still enum-checks on top of that.

Failure types are deliberately distinct rather than one generic error: 06_TESTING_
STRATEGY.md Phase 3 requires a timeout to be distinguishable from a parse failure
in the trace, since they call for completely different fixes.
"""

import json
import re

try:
    import ollama
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise ImportError(
        "The 'ollama' package is required for Phase 3 model calls.\n"
        "Fix: pip install -r requirements.txt"
    ) from exc

import httpx


class ModelError(Exception):
    """Base for every model-call failure. Carries a stable `reason` prefix."""

    reason = "model_error"


class ModelTimeout(ModelError):
    """The model did not respond inside the configured timeout."""

    reason = "timeout"


class ModelUnavailable(ModelError):
    """The server is unreachable, or the model is not pulled."""

    reason = "unavailable"


class ModelParseFailure(ModelError):
    """The response contained nothing parseable as a JSON object (Section 3.6)."""

    reason = "parse_failure"


_CLIENT_CACHE = {}


def get_client(runtime):
    """Return a cached ollama.Client for this runtime config.

    Cached so a run reuses one HTTP connection pool instead of building a client
    per persona per tick.
    """
    host = runtime.get("host", "http://localhost:11434")
    timeout = float(runtime.get("timeout_s", 120))
    key = (host, timeout)
    if key not in _CLIENT_CACHE:
        _CLIENT_CACHE[key] = ollama.Client(host=host, timeout=timeout)
    return _CLIENT_CACHE[key]


def reset_client_cache():
    """Drop cached clients. Used by tests that swap hosts."""
    _CLIENT_CACHE.clear()


def chat(model, system_prompt, user_payload, runtime=None, client=None):
    """One model call. Returns raw response text.

    `user_payload` is serialised to JSON if it is not already a string, so
    personas can hand over the state object directly.
    """
    runtime = runtime or {}
    client = client or get_client(runtime)

    content = user_payload if isinstance(user_payload, str) else json.dumps(user_payload)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    kwargs = {"model": model, "messages": messages}
    if runtime.get("format"):
        kwargs["format"] = runtime["format"]
    if not runtime.get("think", False):
        kwargs["think"] = False

    try:
        response = client.chat(**kwargs)
    except ollama.ResponseError as exc:
        message = (getattr(exc, "error", "") or "").lower()
        # Some models reject the think parameter outright instead of ignoring it.
        # Retry once without it rather than failing the tick over a flag.
        if "think" in message and "think" in kwargs:
            retry = dict(kwargs)
            retry.pop("think")
            try:
                response = client.chat(**retry)
            except ollama.ResponseError as inner:
                raise _response_error(inner, model) from inner
        else:
            raise _response_error(exc, model) from exc
    except httpx.TimeoutException as exc:
        raise ModelTimeout(f"no response from {model} within the configured timeout") from exc
    except (ConnectionError, httpx.ConnectError, ollama.RequestError) as exc:
        # ollama raises the BUILTIN ConnectionError when the server is down -
        # catching only httpx/ollama errors lets a raw traceback escape. Phase 0
        # found this the hard way.
        raise ModelUnavailable(
            f"cannot reach the model runtime: {type(exc).__name__}: {exc}"
        ) from exc

    return (response.message.content or "")


def _response_error(exc, model):
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "error", str(exc))
    if status == 404:
        return ModelUnavailable(f"model {model!r} is not pulled (404): {detail}")
    return ModelUnavailable(f"model runtime rejected the request (HTTP {status}): {detail}")


def _unescape_stray_quotes(text):
    r"""Repair the over-escaping quirk measured on qwen3.5:9b.

    The model sometimes emits a backslash before quotes that are NOT inside a
    string, e.g.

        {"line": null, "reason": \"no line needed\"}

    which is invalid JSON, and which `format="json"` does not prevent. It caused
    10 of the 16 persona failures in the Phase 4 baseline (~7% of dialogue-line
    calls). Section 3.6 sanctions a *permissive extractor*; this stays inside
    that remit and is emphatically not the custom grammar/logits layer Section
    3.6 rules out.

    Safe by construction: repairs are only attempted on text that has ALREADY
    failed a normal parse, and a repaired candidate is only accepted if it then
    parses. Well-formed JSON containing legitimately escaped quotes parses on the
    first attempt and never reaches this function.
    """
    return text.replace('\\"', '"')


_REPAIRS = (_unescape_stray_quotes,)


def _try_repairs(text):
    """Return a parsed dict from a repaired candidate, or None."""
    for repair in _REPAIRS:
        candidate = repair(text)
        if candidate == text:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # Also try the repair against just the outermost object, in case the
            # response wrapped it in prose.
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if not match:
                continue
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_json(text):
    """Permissive JSON extraction, per Architecture doc Section 3.6.

    Tries a straight parse first, then falls back to the outermost {...} block so
    that stray prose or a markdown code fence around otherwise-good JSON does not
    cost a tick. Raises ModelParseFailure rather than returning None so the
    harness records a distinguishable reason (06 Phase 3).
    """
    if text is None:
        raise ModelParseFailure("no response text")
    stripped = text.strip()
    if not stripped:
        raise ModelParseFailure("empty response")

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
        raise ModelParseFailure(f"top-level JSON is {type(parsed).__name__}, expected object")
    except json.JSONDecodeError:
        pass

    repaired = _try_repairs(stripped)
    if repaired is not None:
        return repaired

    # Greedy: from the first { to the LAST }, so nested objects survive.
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        # Distinguish "started an object and got cut off" from "never emitted
        # JSON at all". They look identical in a bare error string but call for
        # different fixes: a token/length limit versus a model ignoring the
        # JSON-only instruction.
        if "{" in stripped:
            raise ModelParseFailure(
                f"malformed JSON: object opened but never closed, in {stripped[:80]!r}"
            )
        raise ModelParseFailure(f"no JSON object found in {stripped[:80]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModelParseFailure(f"malformed JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ModelParseFailure(f"extracted JSON is {type(parsed).__name__}, expected object")
    return parsed


def call_json(model, system_prompt, user_payload, runtime=None, client=None):
    """chat() + extract_json(), the combination every real persona wants."""
    raw = chat(model, system_prompt, user_payload, runtime=runtime, client=client)
    return extract_json(raw)
