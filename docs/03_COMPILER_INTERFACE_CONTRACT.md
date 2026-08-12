# Pixel Swarm — Interface Contract with Pixel-World Compiler (v1)

This document defines the boundary between the Pixel Swarm harness and the renderer on the other side of it.

> **Premise correction, 2026-08-01.** This doc was written against "your existing Pixel-World Compiler". **No such system existed** — it was never built, and nothing by that name is downloadable; the name originates in these planning docs. Every `[TODO]` below was therefore a question about a system that was not there, which is why they survived unanswered until Phase 5. A minimal renderer (`pixel_world/`) now stands in for it and all of them are answered. See Architecture doc Section 8 for the structure and for the acceptance-criterion limitation this creates.

## 1. Principle
The swarm harness and the compiler are two separate systems connected by **one narrow interface** in each direction:
- **In:** Event Feed → Harness (things happening in the world/stream/game)
- **Out:** Harness → Directive → Compiler (what the character should do)

The compiler's core rendering logic is **not modified**. A new thin adapter layer is added on the compiler side that accepts Directives and maps them to whatever the compiler already understands (sprite states, animation clip names, etc.).

## 2. Inbound: Event Feed → Harness

### 2.1 Event schema (harness input)
```json
{
  "type": "chat_hype_spike",
  "intensity": 0.8,
  "ts": 123.45,
  "meta": { "source": "scripted_demo" }
}
```
- `type`: fixed enum for v1 — **confirmed and locked 2026-07-30**: `["chat_hype_spike","chat_calm","game_danger","game_safe","idle_timeout"]`. No expansion needed: these are exactly the five event types the Demo Script's sequence table uses (verified by extracting every event name from `04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md` Section 1 — `chat_calm` ×3, `chat_hype_spike` ×2, `game_danger` ×3, `game_safe` ×1, `idle_timeout` ×1). This closes the Section 6 pre-coding checklist item about enum/sequence mismatch, which that checklist calls the most common source of "nothing happens" bugs. `scripts/validate_configs.py` (Phase 1) must assert this list and `events/demo_sequence.json` still agree.
- `intensity`: float 0–1, lets personas reason about magnitude, not just presence/absence. Only `chat_hype_spike` and `game_danger` carry a graded intensity in v1; the Demo Script table states values for those two and leaves the rest blank. **Convention, decided 2026-07-30 and CORRECTED 2026-07-31:** presence-only signals (`chat_calm`, `game_safe`, `idle_timeout`) use **`intensity: 0.1`**. The field is never optional, so the harness and personas can rely on it existing.

  The original choice of `1.0` — "this signal is fully present", so `chat_calm` at 1.0 means chat *is* calm — was internally reasonable but conflicted with a different doc: the mood-picker prompt (Architecture Section 3.5) reads intensity as **strength**, saying "a strong one (intensity 0.8 or above) is excited". So the event meaning *nothing is happening* was arriving as the strongest possible signal, and the mood-picker moved off `idle` accordingly. Measured on the t=15 tick, varying only this number:

  | `chat_calm` intensity | mood = `idle` |
  |---|---|
  | 1.0 | 6/8 |
  | 0.5 | 4/8 |
  | **0.1** | **8/8** |
  | 0.0 | 8/8 |

  `0.1` rather than `0.0` keeps the original objection satisfied — the value still reads as "an event occurred, of negligible magnitude" rather than inverting to *not* calm or *not* safe — while removing the false strength signal.

  **This was the cause of Demo Script acceptance criterion 4 sitting at 7–8/10 across four different model configurations.** It was a conflict between two documents, not a model weakness, which is why three separate prompt revisions failed to move it. Worth remembering as a class of bug: each doc was locally sensible, and only the number's journey from one to the other was wrong.
- `ts`: seconds since demo start, used for the "recent events" rolling window in the harness (bounds in Architecture doc Section 2).
- **Ordering, decided 2026-07-30:** events are replayed in ascending `ts`. If two events share a `ts`, preserve their order of appearance in `demo_sequence.json` (i.e. a stable sort on `ts`, never a re-sort that could permute equals). The harness must not assume `ts` values are unique. This resolves ambiguity #3 in `06_TESTING_STRATEGY.md`.

### 2.2 Event source for v1
A scripted event generator (a simple Python script or JSON file replayed on a timer) — **not** a live chat/game connection. This satisfies the PRD's non-goal of "no live integration for v1" while still exercising the full pipeline realistically.

## 3. Outbound: Harness → Directive → Compiler

### 3.1 Directive schema (harness output, compiler input)
```json
{
  "tick": 42,
  "mood": "excited",
  "action": "celebrate",
  "line": "Let's go!!",
  "ts": 124.90
}
```
This is the **only** thing the compiler-side adapter needs to consume. It should map directly onto whatever the compiler currently uses to select a sprite/animation state.

### 3.2 Compiler-side state representation — ANSWERED 2026-08-01

**These four questions were unanswerable for the whole project, and it is worth recording why: the Pixel-World Compiler did not exist.** Every doc referred to "your existing compiler"; there was no such system, and nothing by that name is downloadable — the name originates in these planning docs. So Phase 5 was specified against a premise that was never true, and these `[TODO]`s were questions about a system that was not there.

A minimal renderer (`pixel_world/`) now stands in for it, so PRD success criterion 4 can be met rather than abandoned. See Architecture doc Section 8 for the structure and for the honest limitation this creates. Answers against that renderer:

- **What selects the frame-set?** `pixel_world.Renderer.render(mood, action, line=..., frame=...)`.
- **Keyed by named states or IDs?** Named states — plain strings, exactly the enum values in `config/personas.json`. No numeric IDs, no file paths, so the adapter is a rename rather than a lookup table.
- **One-off actions vs sustained loops?** Every action is a short frame cycle that is held for as long as it is asked for. The distinction collapses, so Pixel Swarm's assumption that both are possible holds trivially and no extra signalling is needed.
- **Dialogue-bubble rendering?** Yes, built in — `line` becomes a speech bubble. No core work was required, adapter-only or otherwise.

### 3.3 Adapter responsibility (new code, thin layer only)
The adapter's only job: receive a Directive JSON, translate `mood`/`action`/`line` into whatever the compiler's existing animation-selection API expects, and call it. No decision-making logic belongs in the adapter — all decisions were already made by the harness upstream.

**"No decision-making" does not mean "no validation" — resolved 2026-08-01, closing open ambiguity #2 in `06_TESTING_STRATEGY.md`.** The adapter **rejects and raises** on an out-of-spec Directive rather than passing it through. The reasoning is the same one behind the trace log: a renderer handed an unknown state would silently hold its previous frame, which looks *identical on screen* to a tick that legitimately changed nothing. Failing loudly at the boundary keeps a rendering bug distinguishable from a swarm decision. Choosing which action to draw is a decision; refusing to draw a state that does not exist is not.

## 4. Timing/sync contract
- The compiler is expected to render at its own frame rate independent of tick rate; a Directive stays "active" until the next Directive replaces it.
- **Answered 2026-08-01: no explicit signal is needed — hold is the default.** `RenderAdapter` retains the last Directive and keeps rendering it until another arrives, so a Directive stays active exactly as this section requires. There is no "keep going" message and nothing to time out. Verified by `tests/integration/test_adapter.py::test_adapter_holds_the_last_directive`.

## 5. File location
The adapter lives at `compiler_adapter/adapter.py` (per the repo structure in the Architecture doc, Section 8). It imports/calls into your existing Pixel-World Compiler code — it does not live inside the compiler's own codebase, keeping the two systems cleanly separated and the "no changes to core rendering" acceptance criterion easy to verify by diff.

## 6. Pre-coding checklist (do this before writing any adapter code)
- [x] Section 3.2's four questions are answered in this document, not left as open questions during coding. *(Done 2026-08-01 — and note they were unanswerable until then because the compiler they asked about did not exist.)*
- [x] You've confirmed, by hand, what a minimal call into your compiler's existing animation-selection function looks like (a one-line manual test is enough). — `pixel_world.Renderer.render(mood, action, line=...)`, keyed by named states. Covered by a test that drives **all 36 mood × action combinations**, which is stronger than a one-line manual check and catches a forgotten mapping at test time rather than during a demo.
- [x] You've decided the answer to Section 4's hold/loop question. — **hold**; the adapter retains the last Directive until another arrives, verified by `test_adapter_holds_the_last_directive`.
- [x] The Event schema (Section 2.1) event `type` enum matches exactly the events used in the Demo Script doc's sequence table — mismatches here are the most common source of "nothing happens" bugs. — enforced by `scripts/validate_configs.py`, which parses doc 04's table and diffs it against the sequence rather than trusting a one-time eyeball.

## 7. What's explicitly NOT in this contract for v1
- No bidirectional feedback (compiler telling the harness "animation X finished playing") — directives are fire-and-forget for v1.
- No multi-character directives — schema assumes exactly one character.
- No frame-level control — the harness reasons in terms of named moods/actions/lines only, never pixels or frames directly.
