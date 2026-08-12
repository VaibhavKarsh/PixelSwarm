"""Phase 0 smoke test: prove we can talk to the local model runtime at all.

Deliberately contains no Pixel Swarm logic - no personas, no state, no JSON
schema enforcement. Its only job is to answer "can Python reach Ollama and get
real generated text back."

It does read the model NAME and host out of config/personas.json when that file
exists, so running it with no arguments tests the model the project actually
depends on. It hardcoded a default long after the project had moved to a
different model, which made a clean pass here say nothing about whether the
real run would work. The config is read defensively: this script must still
work on a bare Phase 0 checkout where no config exists yet.
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import ollama
except ImportError:
    print("FAIL: the 'ollama' package is not installed.", file=sys.stderr)
    print("Fix: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_MODEL = "qwen3.5:2b"
FALLBACK_HOST = "http://localhost:11434"
DEFAULT_PROMPT = "Reply with one short sentence describing a pixel-art character waving."
DEFAULT_TIMEOUT = 120.0


def _from_config():
    """(model, host, source) from config/personas.json, or the fallbacks."""
    path = REPO_ROOT / "config" / "personas.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return FALLBACK_MODEL, FALLBACK_HOST, "built-in default (no readable config)"
    models = cfg.get("models") or {}
    model = models.get("mood") or FALLBACK_MODEL
    host = (cfg.get("runtime") or {}).get("host") or FALLBACK_HOST
    return model, host, "config/personas.json"


DEFAULT_MODEL, DEFAULT_HOST, CONFIG_SOURCE = _from_config()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--think",
        action="store_true",
        help="Allow the model to emit reasoning tokens. Off by default: several local "
        "models are reasoning models and spend 50-100x longer thinking than answering.",
    )
    args = parser.parse_args()

    print(f"host    : {args.host}")
    print(f"model   : {args.model}"
          + (f"   (from {CONFIG_SOURCE})" if args.model == DEFAULT_MODEL else "   (override)"))
    print(f"timeout : {args.timeout}s")
    print(f"think   : {args.think}")
    print(f"prompt  : {args.prompt}")
    print("-" * 60)

    client = ollama.Client(host=args.host, timeout=args.timeout)

    started = time.monotonic()
    try:
        response = client.chat(
            model=args.model,
            messages=[{"role": "user", "content": args.prompt}],
            think=args.think,
        )
    except (ConnectionError, httpx.ConnectError, ollama.RequestError) as exc:
        # ollama's client raises the builtin ConnectionError, not an httpx or ollama
        # error, when the server is unreachable - catching only the latter two lets a
        # raw traceback through.
        print(f"FAIL: could not reach the Ollama server at {args.host}.", file=sys.stderr)
        print(f"  underlying error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Fix: start the Ollama server ('ollama serve') and retry.", file=sys.stderr)
        return 1
    except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
        elapsed = time.monotonic() - started
        print(f"FAIL: no response within {args.timeout}s (waited {elapsed:.1f}s).", file=sys.stderr)
        print(f"  underlying error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Fix: raise --timeout, or try a smaller model.", file=sys.stderr)
        return 1
    except ollama.ResponseError as exc:
        print(f"FAIL: Ollama rejected the request (status {exc.status_code}).", file=sys.stderr)
        print(f"  {exc.error}", file=sys.stderr)
        if exc.status_code == 404:
            print(f"  Fix: the model '{args.model}' is not pulled. Run: ollama pull {args.model}", file=sys.stderr)
            print("  Or list what you already have with: ollama list", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    text = response.message.content or ""
    thinking = getattr(response.message, "thinking", None) or ""

    print("RAW RESPONSE:")
    print(text)
    print("-" * 60)
    print(f"elapsed  : {elapsed:.2f}s  (first call also loads the model into memory)")
    print(f"answer   : {len(text)} chars, {response.eval_count} tokens")
    if thinking:
        print(f"thinking : {len(thinking)} chars  <-- reasoning tokens, not part of the answer")

    if not text.strip():
        print("FAIL: the model returned an empty response.", file=sys.stderr)
        if thinking:
            print("  It spent the whole budget on reasoning tokens. Drop --think.", file=sys.stderr)
        return 1

    print("OK: received a non-empty response from the local model runtime.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
