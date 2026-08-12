# Pixel Swarm — Implementation Roadmap (v1)

This roadmap sequences the build into small, independently-verifiable phases, each sized for one or two vibe-coding sessions. Every phase ends with a concrete "definition of done" you can check without reading code — either something runs and prints/logs the right thing, or it doesn't. This is the point: vibe coding drifts when the target is fuzzy, so each phase gives the session a narrow, testable goal instead of "build the whole thing."

**How to use this with a vibe-coding tool (Claude Code, Cursor, etc.):**
- Start each new session by pointing it at all 4 reference docs (PRD, Architecture, Interface Contract, Demo Script) plus this roadmap, and tell it which phase you're on.
- Never let a session jump ahead to a later phase "while it's at it" — if it suggests touching compiler integration during Phase 2, redirect it back to the current phase's scope.
- At the end of every phase, run the phase's Definition of Done yourself before starting the next session. Don't take the model's word that it's done.

---

## Phase 0 — Repo scaffolding & environment check
**Goal:** Empty-but-correct project skeleton, and confirm your local model runtime actually works before writing any swarm logic.

**Tasks:**
1. Create the folder structure exactly as specified in Architecture doc, Section 8.
2. Write a throwaway script that makes one call to your local model runtime (Ollama/llama.cpp/etc.) and prints the response — no Pixel Swarm logic yet, just "can I talk to a local model from Python at all."
3. Initialize git, commit the skeleton.
4. Write a stub `README.md` with the one-line pitch from the PRD.

**Vibe-coding prompt to use:**
> "Set up the repo structure below exactly as given, with empty placeholder files. Then write a single standalone script `scripts/smoke_test_model.py` that sends one prompt to [your model runtime] and prints the raw response. Don't write any Pixel Swarm logic yet." (paste Architecture doc Section 8)

**Definition of done:**
- [x] Folder structure matches Architecture doc Section 8 exactly. — *`scripts/validate_repo_structure.py` passes; it is a command, not a read-through*
- [x] `scripts/smoke_test_model.py` runs and prints a real model response. — *verified against `ornith:9b`; the script reads the model from config so it tests what the project uses*
- [x] Initial commit exists. — *`ae209af`*

**Common failure mode to watch for:** the session tries to install/configure a new inference runtime instead of using the one you already have working from your existing local-model setup. Stop it and point it at what you already use.

---

## Phase 1 — Config files (the enums and rules, before any logic)
**Goal:** `config/personas.json` and `config/transitions.json` exist and are valid, matching Architecture doc Sections 3 and 4 exactly.

**Tasks:**
1. `config/personas.json` — mood enum, action enum, model name(s) to use per persona.
2. `config/transitions.json` — the 2–4 disallowed-transition rules from Architecture doc Section 4.
3. `events/demo_sequence.json` — the full scripted event list from Demo Script doc Section 1, in the Event schema format from Interface Contract doc Section 2.1.
4. Write a tiny validation script (`scripts/validate_configs.py`) that loads all three JSON files and checks they parse and that every `type` in `demo_sequence.json` is one you intend to handle.

**Definition of done:**
- [x] All three config/event JSON files exist and are valid JSON. — *plus a fourth, `events/alt_sequence.json`, added in Phase 8*
- [x] `scripts/validate_configs.py` runs clean. — *on both sequences*
- [x] Manually eyeball `demo_sequence.json` against the Demo Script doc's table — they must match exactly (this is the #1 source of later "nothing happens" bugs, per Interface Contract doc Section 6). — *automated instead of eyeballed - the validator parses doc 04's table and diffs it, so it cannot rot*

---

## Phase 2 — Harness skeleton with mocked personas (no real models yet)
**Goal:** The full tick loop (Architecture doc Section 1) runs end-to-end using **fake/hardcoded persona responses**, proving the loop's control flow, state object, and trace logging work before any model latency/flakiness is in the mix.

**Tasks:**
1. `swarm/state.py` — shared state object (Architecture doc Section 2) as a simple dataclass or dict.
2. `swarm/personas.py` — four functions (`mood_picker`, `action_picker`, `dialogue_line`, `transition_checker`), each returning a **hardcoded/random valid response** matching the output schemas in Architecture doc Section 3. No model calls yet.
3. `swarm/harness.py` — implement the tick loop exactly as in Architecture doc Section 1, calling the (mocked) persona functions, and writing to the trace log per Section 6.
4. Run the full `demo_sequence.json` through the harness with mocked personas and confirm a trace log file is produced with one JSON line per tick.

**Vibe-coding prompt to use:**
> "Implement the tick loop from the Architecture doc's Section 1, using stub persona functions that return valid-but-fake responses matching the schemas in Section 3. Do not call any real model yet — the goal is to prove the loop and trace logging work first." (paste Architecture doc Sections 1, 2, 3, 6)

**Definition of done:**
- [x] Running the harness against `demo_sequence.json` produces a `logs/trace_<timestamp>.jsonl` with one line per tick. — *10 lines for 10 ticks*
- [x] Every trace line has the shape from Architecture doc Section 6. — *asserted per line by `test_harness_loop.py`*
- [x] No crashes across the full scripted sequence. — *20/20 runs complete*

**Why this phase matters for vibe coding specifically:** if you skip straight to wiring real models, and something breaks, you won't know if it's the loop logic or the model call. Proving the loop first with fakes isolates the failure surface for every phase after this.

---

## Phase 3 — Wire in one real persona at a time (mood-picker first)
**Goal:** Replace mocked personas with real local-model calls, **one persona at a time**, so any breakage is attributable to exactly one change.

**Tasks (repeat per persona, in this order: mood-picker → action-picker → dialogue-line → transition-checker):**
1. Implement `swarm/model_client.py` — thin wrapper around your local model runtime (one function: takes a system prompt + input JSON, returns raw text).
2. Replace the mocked `mood_picker` function with a real call using the exact prompt template from Architecture doc Section 3.5.
3. Implement the JSON parsing approach from Architecture doc Section 3.6 (simple regex-extract + `json.loads`, with failure → fallback per Section 5).
4. Run the harness again with mood-picker real and the other three still mocked. Confirm outputs look sane by reading the trace log.
5. Only once mood-picker is confirmed working, move to action-picker, and so on.

**Vibe-coding prompt to use (per persona):**
> "Replace the mocked `mood_picker` function with a real call to [your model runtime], using this exact system prompt: [paste from Architecture doc Section 3.5]. Parse the response as JSON per the approach in Section 3.6, and fall back per Section 5 on any parse failure or timeout. Leave the other three personas mocked for now."

**Definition of done (per persona, x4):**
- [x] That persona's real model call produces valid JSON matching its schema on at least 10 consecutive ticks in a test run. — ***all four** at 10/10, live, re-verified 2026-08-09*
- [x] A deliberately malformed/slow response (simulate by killing the model mid-call or feeding garbage) triggers the documented fallback, not a crash. — *dead-endpoint check plus the full malformed battery via the fake client*
- [x] Trace log confirms the persona's `reason` field is populated and sensible for at least a few ticks you spot-check by hand. — *an empty `reason` is scored as a failure by the live check, not just eyeballed*

**Common failure mode to watch for:** the vibe-coding session tries to "improve" the prompt template on its own. Keep it locked to Architecture doc Section 3.5 during this phase — prompt tuning is a later, explicit phase (Phase 6), not something to drift into now.

---

## Phase 4 — Full swarm integration + conflict resolution proof
**Goal:** All four personas are real, and you can specifically trigger and observe the transition-checker overriding a conflicting proposal (this is your key demo/interview beat).

**Tasks:**
1. Run the full `demo_sequence.json` with all four real personas active.
2. Locate the t=60 tick (the `game_danger` arriving during a celebrate-biased moment, per Demo Script doc Section 1) in the trace log.
3. Confirm the transition-checker's `verdict` is `"reject"` at that tick (or a nearby one) with a sensible `reason`, and that `final_action` differs from what action-picker proposed.
4. If the conflict never actually triggers naturally, deliberately adjust `transitions.json` or the demo sequence's intensities until it does — the conflict moment must be real and reproducible, not hoped-for.

**Definition of done:**
- [x] Full demo run completes with all four personas real (no mocks left). — *20/20*
- [x] At least one specific, identifiable tick in the trace log shows a transition-checker override, matching Demo Script doc's Acceptance Criteria (Functional, item 3). — *t=60, 19/20 runs, scored strictly - a harness fallback does not count*
- [x] At least 3 distinct moods and 3 distinct actions appear across the run (Acceptance Criteria, Functional item 2). — *18/20*
- [x] `chat_calm` at t=15 produces no change (Acceptance Criteria, Functional item 4). — *20/20*

**This phase is the real "is Pixel Swarm working" milestone.** Everything before this is groundwork; everything after this is integration and polish.

---

## Phase 5 — Compiler adapter integration
**Goal:** Directives actually drive the Pixel-World Compiler's rendering, with zero changes to the compiler's core logic.

**Tasks:**
1. Fill in the `[TODO]` items in Interface Contract doc Section 3.2 and 4 using your actual compiler code (do this *before* writing adapter code, not during).
2. Implement `compiler_adapter/adapter.py` per Interface Contract doc Section 3.3 — translate a Directive into whatever call your compiler's existing animation-selection function expects.
3. Run the harness with the adapter wired in, confirm the compiler visibly changes animation state as directives arrive (watch it render, don't just trust logs here).
4. Diff your compiler's core files against their pre-Pixel-Swarm state — confirm zero changes (Demo Script doc, Acceptance Criteria, Integration section).

**Vibe-coding prompt to use:**
> "Implement `compiler_adapter/adapter.py` per the Interface Contract doc's Section 3.3, calling into [paste your compiler's actual animation-selection function signature, now that Section 3.2's TODOs are filled in]. Do not modify any file inside the compiler's own module — only add this new adapter file."

**Definition of done:**
- [x] Compiler visibly renders different animations as directives change during a live run. — *each Directive produces a verifiably different frame; note the renderer had to be written here, see 02 Section 8*
- [x] `git diff` on compiler core files (outside `compiler_adapter/`) is empty. — ***cannot be met as written** - the compiler never existed, so it was written here. Replaced by a checkable equivalent: `pixel_world/` imports nothing from `swarm/`, enforced by an import-graph test*
- [x] Timing/hold behavior matches whatever you resolved in Interface Contract doc Section 4. — *hold-until-replaced, verified by `test_adapter_holds_the_last_directive`*

---

## Phase 6 — Prompt tuning & robustness pass
**Goal:** Now that the system works end-to-end, spend a bounded amount of time making it *reliably* good, not just "worked once."

**Amendment, 2026-07-30 — two prompt changes were pulled forward out of this phase, before Phase 1.** Phase 0's benchmark proved the demo's headline t=60 beat could not happen with *any* installed model under the original prompts: the transition-checker prompt never listed the action enum (models invented actions like `freeze`, `stay_flat`) and never said how to pick a fallback (models defaulted to `current_action`, which at t=60 is itself the forbidden action). Waiting until Phase 6 would have meant validating Phases 3–5 against a system that could not structurally pass Phase 4. Full rationale in Architecture doc Section 3.5a. This is a narrow exception, not a precedent for tuning wording generally during Phases 1–5 — the "keep it locked" rule above still applies to everything else in Section 3.5.

**Tasks:**
1. Run the full demo sequence 5–10 times. Log how often the transition-checker conflict actually triggers, how often any persona falls back due to parse failure, and how often moods/actions feel "wrong" on manual review.
2. Tune prompt wording (Architecture doc Section 3.5) based on real observed failures — not speculative improvements. Change one prompt at a time, re-run, compare.
3. If parse-failure rate is uncomfortably high, revisit Architecture doc Section 3.6's "stricter" option (e.g. your model runtime's JSON mode) — still a config flag, not custom code.
4. Tighten `transitions.json` rules if the conflict moment isn't reliably reproducible across runs.

**Definition of done:**
- [x] 8+ out of 10 runs of the full demo sequence pass all Functional acceptance criteria from Demo Script doc without manual intervention. — ***19/20 (95%)** on the shipped config `d3f620203e0a`, measured 2026-08-11.*
  - **This line used to read "17/20 (85%), two independent n=10 samples at 9/10 and 8/10", and that figure is withdrawn.** It was a real measurement, but three later n=10 samples of one *unchanged* configuration scored 6, 9 and 8 — the same distribution, read three ways. The bar itself ("8+ out of 10") is a threshold on a quantity that n=10 cannot resolve to better than about ±2 runs, so treat any single sample here as directional. See Architecture doc 7.1q and Demo Script Section 2.
- [x] Persona parse-failure rate is low enough that you never see more than one fallback per run in your last 3 test runs. — ***0.00 per run** across the shipped config's 20 runs; 0.04/run across all 50 runs measured that day, all of them timeouts rather than malformed output.*

**Time-box this phase explicitly** (e.g. one focused session) — infinite prompt tweaking is a classic vibe-coding trap. "Good enough to reliably pass the demo" is the bar, not "perfect."

---

## Phase 7 — Recording, trace artifact, and writeup
**Goal:** Produce the actual portfolio deliverables — this phase has no code changes, only capture and writing.

**Tasks:**
1. Do one final clean demo run. Save its trace log (`logs/trace_<timestamp>.jsonl`) as the canonical artifact.
2. Screen-record the run (60–90 sec), showing idle → hype building → the danger conflict moment → recovery → idle, per Demo Script doc Section 3.
3. Write the one-paragraph plain-English architecture writeup (Demo Script doc, Presentation-readiness section) — lead with the conflict-resolution moment, since that's your strongest concrete "here's a real decision the system made" story.
4. Finalize `README.md`: what it is, how to run it, link to the recording, link to the trace log.

**Definition of done:**
- [x] All four Presentation-readiness checkboxes from Demo Script doc are checked. — *there are **three**, not four, in doc 04; all three are ticked*
- [x] Repo has a clean README a stranger could read and understand in under a minute. — *152 words above the fold, ~41s at 220wpm*

---

## Phase 8 — Optional hardening (only if you have runway left)
Not required for v1, but worth knowing what's deliberately deferred rather than forgotten:
- Parallelizing persona calls (Architecture doc Section 1 note) for latency.
- Splitting personas across different model sizes if one persona consistently underperforms.
- A second demo scenario/event sequence to show the system generalizes beyond the one scripted run.
- Basic unit tests around `swarm/harness.py`'s state transitions, if you want the repo to look more rigorous for reviewers.

**Status, closed 2026-08-09.** All four were worked through:

| Item | Outcome |
|---|---|
| Parallelizing persona calls | **Deferred with the number attached.** 57% ceiling measured (33.2s sequential vs 14.3s for the slowest call), but the chain has zero available concurrency — every persona needs its predecessor's output. Extracting it means a different negotiation model, which Section 1 defers to v2. See Architecture doc 7.1o. |
| Splitting personas across model sizes | **Done and reverted.** The condition was met (the checker underperformed), the split was measured, and it cost a 3× latency regression from model-swap thrashing. Architecture doc 7.1g–7.1h. |
| A second demo scenario | **Done — and it failed informatively.** `events/alt_sequence.json` shows the swarm does *not* generalise cleanly: it never returns to idle, and reaches only 2 distinct actions. It also exposed a `"null"`-string bug no test had caught. Architecture doc 7.1n. |
| Unit tests on harness state transitions | **Done.** `tests/unit/test_harness_transitions.py`, 16 tests covering the gaps the existing suite left. |

The second scenario earned its place: it is the only item here that changed what we know about the system rather than confirming it.

---

## Quick reference: phase → doc dependency map

| Phase | Primary doc(s) to have open |
|---|---|
| 0 | Architecture (Sec 8, 9) |
| 1 | Architecture (Sec 3, 4), Interface Contract (Sec 2.1), Demo Script (Sec 1) |
| 2 | Architecture (Sec 1, 2, 3, 6) |
| 3 | Architecture (Sec 3.5, 3.6, 5) |
| 4 | Demo Script (Sec 1, Acceptance Criteria) |
| 5 | Interface Contract (Sec 3, 4, 5, 6) |
| 6 | Architecture (Sec 3.5, 3.6), Demo Script (Acceptance Criteria) |
| 7 | Demo Script (Sec 3, Presentation-readiness) |
| 8 | PRD (Sec 5, non-goals — confirm still out of scope before touching) |
