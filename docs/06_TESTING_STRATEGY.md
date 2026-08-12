# Pixel Swarm — Testing Strategy (v1 Addendum)

This doc supplements the Implementation Roadmap (`05`) — it does not replace its phases or Definition-of-Done checklists, it makes them rigorous. Where this doc adds a test requirement, treat it as part of that phase's Definition of Done, not optional polish. If this doc and `05` ever disagree on what "done" means for a phase, this doc wins for testing scope; `05` still wins for build sequencing.

Read this doc alongside `02_ARCHITECTURE_HARNESS_SPEC.md` (Section 8 now includes a `tests/` folder added on 2026-07-29 for this doc) and `05_IMPLEMENTATION_ROADMAP.md`.

## 0. Why this doc exists

The Roadmap's existing Definition-of-Done checks are mostly "run it once and eyeball the log." That's enough to prove a phase *works*, but not enough to prove it *keeps working* — silent failures, out-of-spec model output, and edge cases in the event stream can all pass a single manual run and still be hiding. This doc's job is to make each phase's failure modes explicit and testable, so "done" means verified, not "seemed fine."

## 1. Testing philosophy for this project

Don't over-build this. Pixel Swarm's own docs are explicit about resisting scope creep (no new frameworks, no infinite prompt tweaking, no systems-research detours) — testing should follow the same discipline. Calibrate rigor by determinism:

- **Deterministic code (state object, JSON parsing/extraction, config validation, harness control flow, adapter mapping) → automated unit/integration tests, run every phase, fast (seconds).** No excuse for these to be hand-verified.
- **Non-deterministic code (actual model output content) → statistical/aggregate testing, not single-run assertions.** Never assert "mood-picker returns X for input Y" against a real model — assert "mood-picker returns a schema-valid, enum-valid response on N/N consecutive calls," and separately track *content* quality by aggregate/manual review (Phase 6).
- **Rendering/visual output → manual verification with a short explicit checklist**, since pixel-level visual diffing is exactly the kind of systems-research scope the PRD's non-goals warn against. Don't build an image-diffing framework for a portfolio demo.

Every phase below sorts its tests into these three buckets so it's clear what should be scripted vs. eyeballed.

## 2. Cross-cutting test infrastructure (build this once, before Phase 2 tests make sense)

### 2.1 Fake model client (`tests/fixtures/fake_model_client.py`)
A drop-in replacement for `swarm/model_client.py` that returns **canned text** instead of calling a real model — including deliberately broken canned responses. This is the single most important piece of test infrastructure in the project: without it, every persona test is slow, flaky, and non-repeatable because it depends on a live local model. Needs to support, at minimum:
- A queue of canned responses (so a test can script "3rd call returns garbage") per persona.
- Canned valid responses for every enum value (mood, action) — used to test the adapter and transition-checker without needing a real model to ever produce e.g. `"angry"`.
- Canned malformed responses: empty string, prose-with-no-JSON, JSON with a value outside the allowed enum, JSON missing a required key, truncated/unterminated JSON, valid JSON wrapped in markdown code fences (a common small-model quirk), a hang/timeout simulation.

### 2.2 Config/schema validators (`tests/unit/test_config_validation.py`, extends `scripts/validate_configs.py` from Phase 1)
Reusable assertions, called both as a standalone script (Phase 1's `validate_configs.py`) and imported into later tests so config drift is caught automatically wherever it happens:
- Every `type` in `events/demo_sequence.json` is in the Interface Contract's event-type enum.
- Every mood/action key referenced in `config/transitions.json` exists in `config/personas.json`'s enums (catches typos in the rule table — this is not currently checked anywhere in the docs).
- Every mood/action enum value in `config/personas.json` has at least one reachable path through `transitions.json` (i.e. no rule accidentally makes a state permanently unreachable).

### 2.3 Golden trace fixtures (`tests/fixtures/golden_trace_*.jsonl`)
After Phase 2 (mocked) and again after Phase 4 (real), save a known-good full trace log. Future phases diff new runs against the golden trace's *structure* (same tick count, same schema per line, same key decision points like the t=60 override) — not exact content, since model output isn't byte-reproducible. This is your regression net for Phase 6 prompt tuning: it catches "fixed parse failures but now nothing ever gets angry."

### 2.4 `tests/` directory layout
```
tests/
├── unit/
│   ├── test_state.py                # state.py dataclass: defaults, rolling window bounds
│   ├── test_json_extraction.py      # the regex-extract + json.loads logic (Section 3.6), in isolation
│   └── test_config_validation.py    # Section 2.2 above
├── integration/
│   ├── test_harness_loop.py         # harness + fake personas/model client
│   ├── test_persona_fallbacks.py    # failure injection per persona (Section 5 of doc 02)
│   └── test_adapter_mapping.py      # every enum value -> compiler call, with a stub compiler function
├── fixtures/
│   ├── fake_model_client.py
│   └── golden_trace_*.jsonl
└── reliability/
    └── (output of scripts/run_reliability_report.py — Section 5 below; gitignored)
```

## 3. Phase-by-phase test plan

Each phase lists what could go silently wrong if this phase ships untested, then the required tests, sorted per Section 1's buckets.

### Phase 0 — Repo scaffolding & smoke test
**Silent failure risk:** smoke test "works" once interactively but breaks in a fresh shell (missing env var, model server not started, wrong working directory assumed).
- **Automated:** a script that diffs the actual folder tree against Architecture doc Section 8's spec (including the `tests/` addition) and fails on any mismatch — this becomes your Phase 0 Definition of Done check, not a manual read-through.
- **Failure injection:** run `scripts/smoke_test_model.py` with the local model server stopped — confirm it prints a clear error and exits non-zero, not a stack trace or a silent hang.
- **Manual:** confirm the smoke test's response is real generated text, not an error string that happens to look like output.

### Phase 1 — Config files
**Silent failure risk:** a typo in an enum value (e.g. `"exicted"`) that only surfaces three phases later as a mysterious fallback, with no indication the root cause was a config typo.
- **Automated:** `scripts/validate_configs.py` (Section 2.2) — valid JSON, event-type/enum cross-references, transitions.json referential integrity.
- **Edge cases (automated):** empty `demo_sequence.json` (harness should run zero ticks cleanly, not crash); a single-event sequence; an event with `intensity` out of `[0,1]` range; two events with identical `ts`.
- **Negative tests (automated):** deliberately break each config file one way at a time (missing required key, wrong type, unknown enum value, duplicate key) and confirm `validate_configs.py` fails loudly and specifically — not just "invalid JSON," but which field and why.
- **Manual:** eyeball `demo_sequence.json` against the Demo Script doc's table once, per existing DoD — this one genuinely needs a human since it's checking intent-match, not structure.

### Phase 2 — Harness skeleton with mocked personas
**Silent failure risk:** the loop "completes without crashing" but is silently doing the wrong thing — e.g. calling personas out of order, leaking state between ticks, or writing a trace log that looks right but is missing a field a later phase depends on.
- **Automated (unit):** `state.py` — default values, `recent_events` window correctly bounded (test at exactly N, N+1, N-1 events), `ticks_since_last_change` increments/resets correctly.
- **Automated (integration):** harness calls the four persona functions in the documented order (mood → action → line → transition-checker) with each seeing the prior outputs — assert via call-order spy, not just final output.
- **Automated (integration):** trace log — every line is valid JSON, every line has exactly the keys in Architecture doc Section 6, `tick` numbers are sequential with no gaps or repeats across a full mocked run.
- **Failure injection:** a mocked persona raises an exception mid-tick — confirm the harness applies the Section 5 fallback default, logs the failure explicitly (not silently), and continues to the next tick rather than aborting the whole run.
- **Edge cases:** zero-event run, and a stress run with a synthetic 500+ event sequence — confirm memory/log growth stays bounded (rolling window is actually rolling, not silently accumulating) and the run still completes.
- **Manual:** none needed — this phase is fully mockable and should be fully automated.

### Phase 3 — Real persona wiring (repeat ×4)
**Silent failure risk:** a model returns *well-formed JSON with an out-of-enum value* (e.g. `{"mood": "neutral", ...}` when `"neutral"` isn't in the allowed list). The docs (Architecture Section 3.6) only define malformed-JSON as a failure case — an out-of-enum-but-valid-JSON response is currently unhandled by the spec. **Flagging this as a gap, not assuming an answer:** decide explicitly whether enum-membership is checked as part of "parse failure" before building this phase, and write it into Architecture doc Section 3.6/5 once decided.
- **Automated (unit, no model needed):** the JSON-extraction function in isolation against the fake client's full malformed-response battery (Section 2.1) — this is where most of the rigor for this phase belongs, and it doesn't need a real model at all.
- **Automated (integration, real model):** for each persona, 10+ consecutive real calls — assert schema validity and enum membership every time; track and print the pass rate.
- **Failure injection (real):** kill/block the model mid-call or point `model_client` at an invalid endpoint — confirm the documented fallback fires and is logged with a distinguishable "timeout" vs. "parse failure" reason (don't collapse both into one generic error).
- **Manual:** spot-check the `reason` field on ~5 real responses per persona for sensibility — this is inherently a judgment call, not automatable.

### Phase 4 — Full swarm integration + conflict resolution proof
**Silent failure risk:** the t=60 conflict "worked once" during development by luck and isn't actually reliably reproducible, so the demo fails live or on the recorded take.
- **Automated:** full real-model run of `demo_sequence.json`, ×10 back-to-back, with a script (not manual reading) that scans each run's trace log and reports: did a transition-checker `reject` occur at/near t=60, did `chat_calm`@t=15 produce zero state change, did the run end idle. This is the same script you'll reuse for Phase 6 — build it here, don't defer it.
- **Automated:** golden-trace structural diff (Section 2.3) against the Phase 2 mocked-run fixture — same tick count and schema shape, now with real content.
- **Edge case:** what happens if the conflict *doesn't* trigger on a given run because the model happened to already be cautious — confirm this is a "recorded as a miss" outcome in the aggregate report, not a crash or a false-positive pass.
- **Manual:** read the actual t=60 trace line by hand once and confirm the override narrative makes sense as an interview story beat — the report can tell you *that* it rejected, only a human confirms it reads well.
- **Known risk carried in from Phase 1 — check this first when the conflict fails to fire.** Phase 0's n=8 probe has the chosen model landing on `angry` instead of `alert` at t=60 in 3 of 8 runs, and `transitions.json` lets `angry` celebrate freely (only `wave` is disallowed). So expect the beat to silently no-op in roughly a third of runs *before* any tuning. The aggregate report should therefore break the conflict-trigger rate down **by which mood was committed at t=60**, not just report an overall pass rate — an overall "6/10" hides whether the misses are all one mood, which is the difference between a one-line `transitions.json` fix and a real problem. Full detail in Architecture doc Section 4.

### Phase 5 — Compiler adapter integration
**Silent failure risk:** the adapter silently no-ops or defaults on a Directive it doesn't fully understand (e.g. an action the compiler-side mapping table forgot to include), and the demo *looks* fine because the previous frame is still showing.
- **Automated:** enumerate every mood/action/line-or-null combination the adapter can receive and assert each maps to a specific, distinct call into a **stubbed** compiler function (dependency-injected or monkeypatched) — this guarantees full mapping coverage without needing to render anything, and catches "forgot to add the mapping for `duck`"-type gaps immediately rather than during a live demo.
- **Automated:** malformed/out-of-enum Directive fed to the adapter — per Interface Contract doc Section 3.3 the adapter has no decision logic, but "no decision logic" should not mean "silently passes garbage to the compiler." Decide and test explicitly whether the adapter rejects-and-logs or trusts-and-passes-through; write the answer into Interface Contract Section 3.3 once decided (currently unspecified).
- **Automated:** a script-level diff of the compiler's core files' content hashes before/after this phase's work — fails loudly if anything outside `compiler_adapter/` changed, operationalizing the "zero core changes" acceptance criterion instead of relying on a manual `git diff` read.
- **Manual (required — can't be meaningfully automated here without over-building):** watch the compiler actually render through one real directive-driven run; confirm the hold/loop behavior resolved in Interface Contract Section 4 matches what's on screen.

### Phase 6 — Prompt tuning & robustness pass
**Silent failure risk:** a prompt tweak that fixes the symptom you were annoyed by while silently regressing something else (e.g. reduces dialogue-line noise but also makes `angry` unreachable) — this is only catchable by comparing aggregate stats before/after, never by a single satisfying-looking run.
- **Automated:** run the Phase 4 reliability script (Section 5 below) once to capture a **baseline** before touching any prompt, then re-run after each individual prompt change and diff the aggregate report against baseline — conflict-trigger rate, per-persona parse-failure rate, and mood/action distribution (coverage across the full enum, not just the happy path) all get tracked, not just the one metric you were trying to fix.
- **Manual:** the existing Roadmap DoD (8/10 runs clean) stays as the human-facing bar; the aggregate script is what actually produces that number instead of manual tallying.

### Phase 7 — Recording, trace artifact, and writeup
No new code, so no new automated tests — but don't skip validation of the deliverable itself:
- **Automated:** run the Section 2.2 config/trace validators against the final canonical trace log before calling it "the" artifact — a schema-broken trace log undermines the explainability claim the whole portfolio piece rests on.
- **Manual:** everything else here is inherently a human deliverable (recording, writeup).

### Phase 8 — Optional hardening
If you do parallelize persona calls, that specifically introduces a new failure class not present anywhere in v1 and needs its own tests before it ships:
- **Automated:** a race-condition test on the shared state object and trace-log writer under concurrent persona calls (e.g. run many ticks concurrently against a fake client with artificial jitter, assert no interleaved/corrupted trace lines and no lost writes).
- Otherwise, per the existing doc: unit tests around `harness.py` state transitions, written once, generically useful regardless of which Phase-8 item you pick up.

### Post-Phase-8 hardening (added 2026-08-11)
Three checks added after a maturity review, each closing a gap that had been found by luck rather than by a test:
- **Prompt semantic coverage** (`scripts/validate_prompt_fidelity.py`) — asserts every value in the mood and action enums has a stated trigger in its prompt, not merely a place in the `Allowed …` list. The review found `alert`, `sad` and `angry` were listed but never defined, which is why the alt scenario once produced `angry` for a *safety* event. Every prompt gap this project hit had that same shape, so it is now a test rather than a discovery.
- **Config fingerprint** (`tests/unit/test_config_version.py`) — asserts the trace stamp changes for models, rules, window and **prompt text**, and does *not* change for documentation-key edits or key reordering. Both directions matter: a fingerprint that misses a real change is a false negative, one that fires on a comment is noise nobody reads.
- **Rules-table budget** (`scripts/validate_configs.py`) — the checker prompt embeds the whole transitions table, and `granite4.1:3b` measurably fell from 8/8 to 0/8 when the table went from one rule to two. The budget makes further growth a deliberate re-measurement rather than a silent regression.
- **Writeup/artifact agreement** (`tests/integration/test_writeup_matches_trace.py`) — `demo/WRITEUP.md` invites the reader to open `demo/trace_canonical.jsonl` and check its claims, so those claims are testable. They were also wrong: the tick table differed in 4 of 10 rows, all three "verbatim" quotes were paraphrases, and the stated action variety was off by one. Prose is not usually testable, but prose *about a JSON file* is, and this is the document a reviewer reads first.

- **The grounded-rejection guard** (`tests/unit/test_grounded_rejections.py`) — Section 5's second invariant. Nine of its twelve tests are about when the guard must **not** fire, because an over-eager override would be worse than the bug it fixes: it would silently discard the checker's genuine work, including the t=60 beat the demo is built on. Covered: grounded rejections honoured, approvals untouched, `harness_fallback` not second-guessed, a failed action-picker leaving nothing to defend, legality judged against the *committed* mood rather than the proposed one, and an exhaustive check that whatever the guard restores survives the illegal-commit invariant for every mood/previous-action pair.
  - Building it also exposed an **overlap between the two invariants** that no test had reason to cover before: when the checker rejects a legal action *and* names an illegal fallback, the guard reaches it first and the invariant never fires. Two existing tests failed on exactly that, correctly. The fix was not to weaken them — they now set `enforce_grounded_rejections: false` so they reach the mechanism they are named after — plus two new tests asserting the **composition**: same property (nothing illegal committed) by either route, with the trace saying which one acted. When two safety mechanisms overlap, test each in isolation *and* test that the property holds when both are live.

**A gap this section should name rather than imply.** The prompt-coverage check verifies every enum value has a trigger; it cannot verify a prompt states its **closure** rule ("anything not listed is allowed"), which 7.1q found was the more damaging omission. That is a semantic property, not a lexical one. What catches it instead is the grounding statistic in `scripts/run_reliability_report.py` — an outcome measure rather than a static check. Both kinds are needed: the static check would not have found 7.1q, and the outcome measure would not have found the missing `alert` trigger.

## 4. Spec ambiguities this pass surfaced (resolve before the relevant phase, don't silently assume)

1. **RESOLVED 2026-07-30.** Valid JSON, invalid enum value → treated as a persona failure, same fallback path as malformed JSON. Now stated explicitly in Architecture doc Section 5.
2. **RESOLVED 2026-08-01.** Adapter behaviour on an out-of-spec Directive → **reject and raise**, not pass through. Now stated in Interface Contract Section 3.3. The deciding argument was that a renderer handed an unknown state silently holds its previous frame, which is visually indistinguishable from a tick that legitimately changed nothing — the same silent-failure class the trace log exists to prevent. Refusing to draw a nonexistent state is not a decision in the Section 3.3 sense; choosing *which* action to draw would be. (Note this stayed open the whole project waiting for "the real compiler", which turned out never to have existed — see Interface Contract Section 3.2.)
3. **RESOLVED 2026-07-30.** Duplicate-timestamp events in `demo_sequence.json` → stable sort on `ts`, preserving file order for ties. Now stated in Interface Contract Section 2.1. (Doesn't currently arise — the hand-authored Demo Script sequence has no duplicate timestamps — but the harness must not assume `ts` is unique.)
4. **RESOLVED 2026-07-30.** How the transition-checker chooses its fallback action, and the identical degenerate case in the harness-level fallback — both were the most consequential of the four. Section 3.4 required a "valid fallback" but never said how to pick one, so every model tested reverted to `current_action`; at t=60 that is `celebrate`, the forbidden action itself, so the intervention silently did not happen. Fixed by (a) adding an explicit fallback rule to the transition-checker prompt (Section 3.5/3.5a) and (b) pinning the harness-level fallback to `idle_loop` instead of "keep current state" (Section 5), plus a harness-level invariant that a committed action must be legal for the committed mood regardless of what any persona returns. Re-measured at n=8 immediately after the prompt change — see Architecture doc Section 7.1c.

5. **RESOLVED 2026-08-01** (fix), confirmed 2026-08-09 (measurement): C4 is now **20/20** and C5 **19/20** across a fresh 20-run baseline, against 7–8/10 and a wildly varying C5 when this was raised. The cause was not the model: presence-only events carried `intensity: 1.0` while the mood prompt read intensity as *strength*, so "nothing is happening" arrived as the strongest possible signal. Setting it to `0.1` fixed both criteria at once. See Architecture doc 7.1j. Original entry follows.
   **Found 2026-07-30 during the Phase 3 live runs.** What `idle` means versus `happy`, and what `idle_timeout` signals to a persona. The mood prompt gives no basis for reading calm or absence-of-stimulus as `idle` rather than `happy`; both are in the enum and both are defensible. Across four full-sequence runs the model answered `chat_calm` at t=15 with `idle` three times and `happy` once, and ended the run on `idle` three times and `happy` once — **no code change between runs**. So Demo Script acceptance criteria 4 and 5 currently pass by luck rather than by design, which is worse than a consistent failure because the demo is recorded once. Evidence in Architecture doc Section 7.1d. Affects Phase 4; the fix is a prompt clause, so it belongs in Phase 4 or 6, not Phase 3.
6. **RESOLVED 2026-07-30** (fix), confirmed 2026-08-09 (measurement): C3 is now **19/20**, against **0/4** when this was raised — the conflict did not fire at all. Fixed by adding a second rule kind to `transitions.json`: `by_previous_action`, a *smoothness* constraint keyed on the pose the character is currently in. The conflict now arises from the personas behaving **correctly** rather than requiring the action-picker to misbehave. See Architecture doc Section 4 and 7.1d(a). Original entry follows.
   **Found 2026-07-30 — the Phase 4 blocker.** The t=60 conflict does not arise when the action-picker is real. It follows the proposed mood and picks `look_around` for `alert`, which is legal, so no conflict exists for the checker to resolve; only the *mock* action-picker produced the "momentum" the Demo Script assumes. Note the Phase 0 probe fed the checker a conflict directly, so it proved the checker *resolves* one — never that one *occurs*. Demo Script acceptance criterion 3 (a visible transition-checker intervention) cannot pass until this is addressed. Roadmap Phase 4 task 4 already anticipates it and authorises adjusting `transitions.json` or the demo sequence's intensities; a prompt change to give the action-picker inertia would instead be Phase 6. Evidence and options in Architecture doc Section 7.1d(a).

**Items 5 and 6 were both resolved on 2026-07-30, before starting Phase 4**, since both blocked Phase 4's acceptance-criteria run:
- **#5** — the mood prompt gained a clause naming calm/quiet/timeout as an absence of stimulus (`idle`) and reserving `happy` for an actual positive event (Section 3.5, rationale in 3.5a).
- **#6** — `transitions.json` gained a second rule kind, `by_previous_action` (Section 4). The conflict now arises from *smoothness* — you cannot cut from `celebrate` straight to `look_around` — rather than requiring the action-picker to misbehave. This also removed the mock action-picker's artificial "momentum" special case, which had been making the mock behave *unlike* the real model at the single most important tick.

**All six are now closed** (item 2 in Phase 5, items 5 and 6 confirmed by measurement on 2026-08-09). Every one turned out to be a genuine specification gap rather than a model weakness — something the docs assumed and never stated — which is the pattern worth carrying into any future work here: when the swarm behaves "wrongly", read its `reason` fields before reaching for a different model.

**Diagnosed 2026-07-30, for Phase 6 — the dominant parse failure has a single identified cause.** 10 of the 16 persona failures in the Phase 4 baseline were `line:parse_failure`. Reproduced at 2/30 (~7%) with a direct probe. The model over-escapes: it emits

```
{"line": null, "reason": \"Mood and action shifted; no line needed yet.\"}
```

— backslash-escaped quotes *outside* a string context, which is invalid JSON, so `json.loads` fails at the `\`. Ollama's `format="json"` does not prevent it. Two options for Phase 6, and **this is a decision to make deliberately rather than drift into**:
- **Repair it in the extractor** — after a normal parse has already failed, retry with `\"` unescaped. Narrow and safe (it only runs on input that already failed), but the Roadmap's Phase 6 wording explicitly cautions "still a config flag, **not custom code**", so this cuts against the doc's stated preference.
- **Leave it to the fallback path.** A failed dialogue line costs nothing visible — Section 3.3 wants `null` most ticks anyway, and the fallback supplies exactly that. On this reading the ~7% failure rate is cosmetic noise in the trace rather than a defect worth custom parsing code for.

**A note on how #6 was found, because the lesson generalises.** Every per-phase gate passed while the project's headline acceptance criterion was unreachable. Phase 0's probe fed the checker a conflict directly and proved it *resolves* one; nothing tested whether a conflict *occurs*. That gap survived Phases 0–3 and only surfaced when all four personas ran as a chain. When a test constructs the very condition it is checking for, it validates the handler, not the system — Phase 4's checks must assert the conflict arises from an unmodified run, not from an injected fixture.

**Consequence for Phase 4's test (item 4 above).** The Roadmap's Phase 4 wording — "`final_action` differs from what action-picker proposed" — is too weak a bar and will report a false pass. `reject` with `final_action: "jump"` under mood `alert` differs from the proposed `celebrate`, yet `jump` is *also* disallowed for `alert`, so the harness could never legally commit it. Phase 4's automated check (and `scripts/benchmark_models.py`, which already does this) must require the fallback to be **legal for the final mood per `transitions.json`**, not merely different. Two separate models produced exactly this false pass during Phase 0.

## 4b. Phase 6 log — what was tried, what it cost, and one methodology mistake

Kept here rather than in the Architecture doc because it is about *how the measurement was run*, and the mistake is the reusable part.

**Cycle 1 changed two things at once, which was a mistake.** The Roadmap says "change one prompt at a time, re-run, compare." I bundled the JSON-extractor repair together with a mood-prompt clause, so the criteria movements were confounded between them. One effect was still cleanly attributable — `line:parse_failure` went 10 → 0, which nothing but the extractor repair could explain — but the criteria drops (C2 10→8, C3 7→5, C5 7→5) could not be assigned with confidence. Do not bundle changes, even when one of them looks obviously safe.

**Verdicts:**
- **JSON extractor repair — KEPT.** Eliminated the single largest failure source. Failure rate 1.60 → 1.20 per run.
- **Mood-prompt "absence of stimulus is idle, never happy, never sad" clause — REVERTED.** It targeted C4 (unchanged at 8/10) and C5 (worse), and the C2 drop has a mechanical explanation rather than merely a correlational one: instructing the model to withhold two of six moods narrows the range, and C2 counts distinct moods. A clause that improves a criterion by restricting the character's expressive range is the wrong trade for this project anyway.

**The measurement is expensive and that shapes what is worth trying.** A 10-run report is ~55 minutes of wall clock (mean 330s/run, worst observed 620s under load). At that cost, speculative wording changes are not affordable; only changes with a diagnosed mechanism are.

**The remaining gap is not obviously a prompt problem.** C3's misses are dominated by the transition-checker mis-judging the t=60 tick — approving an illegal `celebrate → look_around` outright, or rejecting correctly and then naming an illegal fallback. Three prompt revisions across Phases 3–6 have not moved that class of error, while the deterministic Section 5 invariant has caught **every** instance. Before spending further cycles on wording, try the lever Section 7 explicitly sanctions and that has never been tested: **give the transition-checker a different model**. `config/personas.json` already holds a model name per persona, so it is a config change, and Section 7's own guidance is to "split personas onto different model sizes/families later if you find a specific persona needs more capability" — which is exactly what the measurement now shows.

## 5. Reliability report script (`scripts/run_reliability_report.py`)

Built once, used by both Phase 4 and Phase 6 (Section 3 above references this repeatedly — build it in Phase 4, not twice):
- Runs the full real-model demo sequence N times (default 10).
- Per run, records: pass/fail on each Demo Script Functional acceptance criterion, per-persona parse-failure count, and full mood/action enum coverage.
- Outputs an aggregate summary (`tests/reliability/report_<timestamp>.json` + a printed table) — this is what turns Phase 6's "8/10 runs pass" from a manual tally into a one-command check, and gives you a real number to quote in the portfolio writeup ("passes N/10 runs") instead of an anecdotal claim.

**n=4 is not enough to decide anything — measured 2026-07-30.** Two runs of `scripts/benchmark_models.py --runs 4`, same script, same machine, same day, disagreed wildly on the same probe:

| Model | mood-shift, run 1 | mood-shift, run 2 |
|---|---|---|
| `qwen3.5:2b` | 0/4 | 3/4 |
| `nemotron-3-nano:4b` | 2/4 | 4/4 |

Those two runs would have produced opposite model-choice decisions. Consequences to carry forward:
- Treat any single small-n probe result as a *hint*, never as settled — this is exactly the trap Section 1 warns about, and Phase 0 fell into it once already (Architecture doc Section 7.1's original 3-model conclusion).
- Prefer n≥10 for any number that gates a decision, and report the spread (min/max or the raw per-run list), not just the mean or a bare pass/fail.
- A strict-majority pass threshold over 4 runs is near-coin-flip for a model that genuinely sits around 50%. Either raise n or report the raw distribution and judge by eye — do not let a 3-vs-2 split look authoritative.
