# Pixel Swarm — Architecture & Harness Spec (v1)

This is the reference document for how the swarm is structured, what each persona receives and returns, and how the harness resolves disagreement. Any coding session should treat this doc as the source of truth — if code and this doc disagree, update this doc first, then the code.

## 1. High-level loop

```
every tick (triggered by a new event OR a fixed timer):
  1. Harness gathers: current_state + new_event(s) since last tick
  2. Harness calls Mood-picker      -> proposed_mood
  3. Harness calls Action-picker    -> proposed_action   (sees proposed_mood)
  4. Harness calls Dialogue-line    -> proposed_line      (sees proposed_mood + proposed_action, may be null)
  5. Harness calls Transition-checker -> verdict: {approve} or {reject + fallback}
  6. Harness commits final_state = approved (mood, action, line)
  7. Harness emits a Directive to the Pixel-World Compiler adapter
  8. Harness logs the full tick (inputs, all proposals, verdict, final state) to the trace log
```

Personas are called **sequentially, not in parallel**, in v1 — mood informs action, action informs dialogue, and the transition-checker sees all three. Parallelizing is a valid v2 optimization but adds negotiation complexity not needed for the demo.

## 2. Shared state object (passed to every persona call)

```json
{
  "current_mood": "idle",
  "current_action": "idle_loop",
  "last_line": null,
  "ticks_since_last_change": 4,
  "recent_events": [
    {"type": "chat_hype_spike", "intensity": 0.8, "ts": 123.4},
    {"type": "game_danger", "intensity": 0.5, "ts": 124.1}
  ]
}
```

- `recent_events` is a short rolling window — not the full history. This keeps prompts small and keeps personas reacting to "what just happened," not the whole session.
- **Window size, decided 2026-07-30 (was "e.g. last 5–10 events or last N seconds"):** keep **at most 5 events, and drop anything older than 60 seconds** — whichever limit binds first. Store both as `recent_events_max_count: 5` and `recent_events_max_age_s: 60` in `config/personas.json` so Phase 1 owns the values and they are tunable without code changes.
  Rationale: the Demo Script spaces events ~15s apart, so 60s admits about four events. That is deliberately wide enough that at t=60 the window still holds the two `chat_hype_spike` events *and* the new `game_danger` — the celebratory momentum has to still be visible for the t=60 conflict to exist at all. A much tighter window would make the headline beat disappear for an uninteresting reason. The count cap is the safety net for any future denser event stream.
- This object is the **only** thing personas see of the world. No persona talks to the compiler directly.

## 3. Persona contracts

Each persona is a single local-model call with a fixed input schema and a fixed, constrained output schema (JSON). Keep prompts short and single-purpose — one job per persona, not "you are a helpful animation assistant."

### 3.1 Mood-picker
- **Input:** shared state object (section 2).
- **Output:**
```json
{ "mood": "excited", "confidence": 0.0-1.0, "reason": "short string" }
```
- **Allowed values:** fixed enum, e.g. `["idle","happy","excited","alert","sad","angry"]` — defined once in a shared config, not re-invented by the model.
- **Reason field** is mandatory — this is what makes the tick log explainable later (PRD success criterion #5).

### 3.2 Action-picker
- **Input:** shared state object + `proposed_mood` from step 2.
- **Output:**
```json
{ "action": "celebrate", "confidence": 0.0-1.0, "reason": "short string" }
```
- **Allowed values:** fixed enum, e.g. `["idle_loop","wave","jump","duck","celebrate","look_around"]`.
- Action-picker should be given a small lookup of "moods that typically pair with this action" in its prompt, but it does **not** enforce validity itself — that's the transition-checker's job (separation of concerns, so debugging is easier).

### 3.3 Dialogue-line
- **Input:** shared state object + `proposed_mood` + `proposed_action`.
- **Output:**
```json
{ "line": "string or null", "reason": "short string" }
```
- Should return `null` most ticks (talking every tick is noisy/unrealistic) — bias the prompt toward silence unless mood/action just changed meaningfully.

### 3.4 Transition-checker (the arbiter)
- **Input:** shared state object + all three proposals from steps 2–4.
- **Output:**
```json
{
  "verdict": "approve" | "reject",
  "final_mood": "...",
  "final_action": "...",
  "final_line": "... or null",
  "reason": "short string explaining approval or what was overridden and why"
}
```
- On `reject`, this persona **must** supply a valid fallback (final_mood/action/line), not just a rejection — the harness should never be left without a committable state.
- **⚠ Measured 2026-07-30 — "valid fallback" is currently underspecified, and it breaks exactly at t=60.** Nothing here or in Section 3.5 tells the checker *how to choose* the fallback action. Every model tested defaults to reverting to `current_action`. That is fine on a normal tick, but at t=60 `current_action` **is** `celebrate` — the very action the `alert` rule forbids — so "revert to current" returns the forbidden action and the intervention silently does not happen. Isolated with a controlled A/B (identical proposals, only `current_action` differing), 5 models, 3 runs each:

  | Model | A: `current_action=celebrate` | B: `current_action=idle_loop` |
  |---|---|---|
  | `gemma4:e2b` | `celebrate` 3/3 — illegal | `idle_loop` 3/3 — legal |
  | `ornith:9b` | `celebrate` 2/3 — illegal | `idle_loop` 3/3 — legal |
  | `qwen3.5:9b` | `defensive_stance`/`idle`/`jump` 3/3 — all illegal | `idle_loop` 3/3 — legal |
  | `granite4.1:3b` | `celebrate` 3/3 (reverts mood too) | mixed |
  | `nemotron-3-nano:4b` | inconsistent | inconsistent |

  The models' *reasoning* is correct in both columns — they cite the rule accurately ("Celebrating while alert is disallowed"). Only the fallback *selection* collapses. So this is a prompt/spec gap, not a model-capability gap, and it is not fixable by swapping models.

  **Do not fix this by editing Section 3.5's prompt yet** — the Roadmap reserves prompt changes for Phase 6 and forbids drifting into them earlier. The likely fix is one added line in the transition-checker prompt ("the fallback action must be one that is permitted for final_mood; never return a disallowed action; default to `idle_loop`") plus a harness-side assertion that a committed action is legal for the committed mood. Decide it deliberately when Phase 6 arrives; until then Phase 4's conflict proof must check the fallback is *legal*, not merely *different*.
- Validity rules (e.g. "sad → celebrate is not allowed in one tick, must pass through neutral first") live in a small config table (section 4), and the transition-checker's prompt should be given that table directly rather than expected to memorize it.

## 3.5 Concrete prompt templates (starting point — tune wording during build, don't redesign structure)

Use these as literal starting prompts. Keep the JSON-only instruction and the enum list explicit every time — small local models drift without repetition.

**Mood-picker system prompt:** *(revised 2026-07-30 — added the urgency line; see 3.5a)*
```
You are the mood-picker for a pixel-art character. Given the character's
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
{"mood": "<one of the allowed moods>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}
```

**Action-picker system prompt:**
```
You are the action-picker for a pixel-art character. Given the character's
current state, recent events, and the mood that was just decided, choose
what the character should be doing right now.

Allowed actions (choose exactly one): idle_loop, wave, jump, duck, celebrate, look_around

Typical mood-action pairings (a guide, not a hard rule):
idle -> idle_loop | happy -> wave | excited -> celebrate or jump
alert -> look_around or duck | sad -> idle_loop | angry -> idle_loop

Respond with ONLY valid JSON, no other text:
{"action": "<one of the allowed actions>", "confidence": <0.0-1.0>, "reason": "<short reason, max 15 words>"}
```

**Dialogue-line system prompt:**
```
You are the dialogue-line picker for a pixel-art character. Given the
character's mood and action just decided, optionally propose a very short
spoken line (under 8 words). Most ticks should have NO line — only propose
one if mood or action just changed meaningfully.

Respond with ONLY valid JSON, no other text:
{"line": "<short line or null>", "reason": "<short reason, max 15 words>"}
```

**Transition-checker system prompt:** *(revised 2026-07-30 — added the enums and the fallback rule; see 3.5a)*
```
You are the transition-checker for a pixel-art character animation system.
You receive the character's current committed state and three proposals
(mood, action, line) from other decision-makers. Check the proposals
against the disallowed-transition rules below. If all proposals are valid,
approve them as-is. If not, reject and supply a valid fallback.

Allowed moods (final_mood must be exactly one of these):
idle, happy, excited, alert, sad, angry

Allowed actions (final_action must be exactly one of these):
idle_loop, wave, jump, duck, celebrate, look_around

Disallowed transitions:
{{insert current contents of transitions.json here}}

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
{"verdict": "approve" or "reject", "final_mood": "...", "final_action": "...",
 "final_line": "... or null", "reason": "<short reason, max 20 words>"}
```

### 3.5a Why these two prompts were revised before Phase 1 (2026-07-30)

Both changes fix defects that Phase 0 measurement proved, and both were made *before* Phase 1 rather than in Phase 6 — a deliberate amendment to the Roadmap, recorded in `05_IMPLEMENTATION_ROADMAP.md` under Phase 6. Neither is prompt *tuning*; the ban on drifting into tuning still stands.

- **Transition-checker: the allowed-action enum was missing entirely.** Section 3.5's own opening instruction says to "keep the JSON-only instruction and the enum list explicit every time," and this prompt was the one that didn't. Models duly invented `freeze`, `stay_flat`, `defensive_stance`, `none`, `idle` — values the harness could never commit. Adding the enum makes the prompt obey the rule the doc already stated.
- **Transition-checker: "supply a valid fallback" never said how.** Documented in Section 3.4. Models defaulted to reverting to `current_action`, which at t=60 is the forbidden `celebrate`, so the intervention silently didn't happen for *any* model. The added paragraph closes that gap.
- **Mood-picker: the demo's core intent was absent.** The prompt gave no basis for ranking a `game_danger` event against two recent `chat_hype_spike` events, so models that stayed `excited` at t=60 were not being unreasonable — the information simply wasn't there. The added line states the priority as a general principle ("threat outranks hype") rather than a hardcoded rule about specific event types, so the swarm still does the deciding.

**This last one is a product decision, not just a bug fix** — it asserts that a reactive character should prioritize danger over crowd hype. That is what the Demo Script intends, but if you disagree with baking that in, this is the line to revisit.

**Second revision, 2026-07-30 (after the Phase 3 live runs, before Phase 4).** Two further changes, both closing gaps that measurement — not speculation — showed were blocking Demo Script acceptance criteria:

- **Mood-picker: an `idle` vs `happy` clause.** Nothing distinguished "nothing is happening" from "something mildly good is happening", so across four identical runs `chat_calm` at t=15 produced `idle` three times and `happy` once, and the run ended `idle` three times and `happy` once. Criteria 4 and 5 were passing *by luck*. The added sentences name calm/quiet/timeout as an absence of stimulus and reserve `happy` for an actual positive event. This is a product decision in the same way the urgency line was: it asserts the character idles rather than beams when nothing is going on.
- **Transition-checker: the two rule kinds.** `transitions.json` gained `by_previous_action` (Section 4), so the prompt has to explain both and require that a fallback break neither. Without this the checker would enforce only half the table.

Both were made before Phase 1's model choice is re-validated in Phase 4, and both are re-measured rather than assumed — see 7.1e.

**Third revision, same day — the second revision caused a regression, and this is why every prompt change gets re-measured.** The first `idle`/`happy` wording ("Use idle when nothing notable is happening… Reserve happy for an actual positive event") over-corrected: across three runs the mood-picker began reading a `chat_hype_spike` as *nothing notable*, twice answering `idle` with reasons like "No threat or significant event; recent calm chat overrides…". The character then never reached `celebrate` before t=60, so the smoothness rule had no `celebrate` to fire on and the headline conflict appeared in only 1 run of 3.

Two things were wrong and both are now fixed in the wording above:
- **Calm was allowed to outweigh a live event.** The clause now says explicitly that a hype or positive event *is* something happening and must never be read as `idle`; `idle` requires calm *with no other recent event*.
- **`happy` versus `excited` was never specified at all** — the same class of gap as `idle` versus `happy`, just unnoticed because nothing had depended on it before. The mood enum is an intensity ladder and the prompt never said so, which is why t=45 produced `excited`, `happy` and `happy` across three identical runs. The clause now ties the positive moods to event intensity, matching the Demo Script's own table (t=30 at 0.6 → "happy/excited", t=45 at 0.9 → "escalates to excited").

The general lesson, worth keeping in mind for Phase 6: **a prompt clause that constrains one decision can silently shift an unrelated one.** Nothing about the `idle`/`happy` fix suggested it would change how hype spikes are read. Only a full re-run showed it. Change one clause at a time and re-measure the whole sequence, never just the behaviour you were aiming at.

## 3.6 Enforcing valid JSON output
Small local models will occasionally return malformed JSON or add stray text. Two acceptable v1 approaches, pick one before building:
- **Simple (recommended for v1):** parse with a permissive JSON extractor (regex out the first `{...}` block, then `json.loads`), and treat a parse failure as a persona failure (see Section 5 fallback handling). This is enough for a demo and keeps the project product-flavored, not systems-research-flavored.
- **Stricter (optional, only if #1 proves too flaky in practice):** use your local inference runtime's built-in JSON-mode / grammar constraint if it has one (e.g. Ollama's `format: json` option) — a one-line config flag, not a custom decoding layer. Do not build a custom grammar/logits system for this project — that's explicitly the kind of systems-research scope this project is meant to avoid.

## 4. Transition validity table (config, not code logic embedded in prompts alone)

Maintain this as a simple data file (e.g. `transitions.json`) both the harness and the transition-checker's prompt can reference:

```json
{
  "by_mood": {
    "sad":   {"disallowed_next_action": ["celebrate", "jump"]},
    "angry": {"disallowed_next_action": ["wave"]},
    "alert": {"disallowed_next_action": ["celebrate", "jump"]}
  },
  "by_previous_action": {
    "celebrate": {"disallowed_next_action": ["duck", "look_around"]}
  }
}
```

There are **two kinds of rule**, and the distinction is the whole reason the transition-checker has anything to do:

- **`by_mood`** — a *pairing* constraint. "While the committed mood is `alert`, the action may not be `celebrate`." Checked against the **final mood** of the tick.
- **`by_previous_action`** — a *smoothness* constraint, and the one Section 3.4 actually describes ("must pass through an intermediate step"). "Coming out of `celebrate`, you may not snap straight to `duck` or `look_around` — pass through something neutral first." Checked against the **currently committed action**, i.e. the state the character is animating *right now*.

Keep this table tiny for v1 — a handful of rules is enough to *demonstrate* the conflict-resolution behavior (PRD success criterion #3), not to build an exhaustive animation rulebook.

**Why `by_previous_action` exists (added 2026-07-30, and it is not optional).** With only `by_mood` rules the transition-checker is very nearly vestigial, and Phase 3's live runs proved it: the action-picker is *given* the proposed mood plus Section 3.5's mood→action pairing guide, so it reliably proposes a mood-appropriate action. A `by_mood` rule can therefore only fire when the action-picker disobeys its own guidance — which the mock did (it simulated "momentum") and the real model does not. Four full-sequence runs with the real action-picker produced **zero** overrides.

A smoothness rule fires on what the personas actually do rather than on them misbehaving. At t=60 the character is mid-`celebrate`, the mood turns `alert`, and the action-picker correctly proposes `look_around` — `by_previous_action.celebrate` blocks the abrupt cut, the checker substitutes `idle_loop`, and the next tick proceeds to `look_around` from a neutral pose. That is a real animation concern (you do not cut from a celebration jump to a crouch in one frame-set), it is exactly the "smooth transition from the current state" Section 3.4 asks for, and it produces the Demo Script's headline beat without anyone having to behave badly.

**Both rule kinds bind simultaneously.** A committed `(mood, action)` pair must satisfy `by_mood[final_mood]` *and* `by_previous_action[current_action]`. At t=60 that leaves `idle_loop` and `wave` legal, and the configured fallback (`idle_loop`) is among them — see Section 5's invariant.

**`jump` carries the same smoothness rule as `celebrate` (added 2026-07-30, from measurement).** Section 3.5's pairing guide offers the action-picker *both* for an excited mood ("excited -> celebrate or jump") and it legitimately picks either. With only `celebrate` covered, whether the t=60 beat happened depended on which one it chose that run — the conflict fired in 3 of 4 runs and the miss was a `jump` run. Cutting from an airborne jump to a crouch is exactly as abrupt as cutting from a celebration, so this completes the rule for the action set the guide actually produces. It is not widened to force a result: `wave` deliberately carries no such rule, because a standing gesture into a scan is not a jarring cut.

**Note added 2026-07-29, partly superseded 2026-07-30** — the reasoning below was sound but the conclusion was incomplete: the `"alert"` rule is necessary and still present (now under `by_mood`), but on its own it does **not** fire at t=60 with a real action-picker, for the reason given in the `by_previous_action` note above. Keep both.

The `"alert"` rule was added deliberately, not part of the original example. The Demo Script's t=60 conflict (Demo Script doc Section 1) depends on `game_danger` pulling the mood toward `alert` (the action-picker's own pairing guide in Section 3.5 already associates `alert` with `look_around`/`duck`, not `celebrate`/`jump`, and the Demo Script's t=75 row confirms `alert` — not `sad` — is the mood that actually settles in). Without an `"alert"` key, the original table (`sad`/`angry` only) would not fire at t=60 at all, since the *current* committed mood going into that tick is `excited`, not `sad` — the demo's headline conflict-resolution moment would have relied entirely on the transition-checker's unaided judgment rather than the documented hard-rule mechanism. Re-verify this table still matches whatever moods your event stream actually produces once real models are wired in (Phase 3+) — this is inference, not a guarantee.

**⚠ Known gap for Phase 4 — `angry` does not forbid `celebrate`.** The n=8 probe in 7.1c shows the chosen model (`qwen3.5:9b`) lands on `angry` rather than `alert` at t=60 in **3 of 8 runs**. `angry` currently disallows only `wave`, so on those runs the proposed `celebrate` violates no rule, nothing fires, and the demo's headline intervention silently does not happen — roughly a third of the time. This is not a bug in the table as specified; it is the table meeting a mood the Demo Script did not anticipate. **Deliberately left unchanged in Phase 1** because Roadmap Phase 4 Task 4 explicitly owns this ("If the conflict never actually triggers naturally, deliberately adjust `transitions.json` or the demo sequence's intensities until it does"). The obvious remedy — adding `celebrate`/`jump` to `angry`'s disallowed list — is also defensible on its own terms, since an angry character celebrating is incongruent. Decide it in Phase 4 with a real multi-run measurement, not on this one probe.

## 5. Failure handling / fallback defaults

- If any persona call times out, errors, or returns malformed JSON: harness substitutes a safe default (`mood: current_mood`, `action: idle_loop`, `line: null`) and logs the failure explicitly — never silently retries indefinitely or crashes the tick loop.
- A substituted fallback is still just a **proposal** — it does not bypass the transition-checker. It flows into step 5 of the loop (Section 1) exactly like a normal proposal would, and can itself be approved or overridden. (Clarified 2026-07-29 — the original wording left this ambiguous.)
- A **valid-JSON-but-out-of-enum** response (e.g. `{"mood": "neutral", ...}` when `"neutral"` isn't in the allowed list) counts as a persona failure and follows this same fallback path — it is not treated as a successful proposal just because it parsed. (Clarified 2026-07-29 — Section 3.6 only mentioned malformed JSON explicitly.)
- Transition-checker is the last line of defense; if *it* fails, the harness falls back to **`mood: proposed_mood` (if it was valid) or `current_mood`, `action: idle_loop`, `line: null`** — and logs a harness-level fallback. **Resolved 2026-07-30:** this used to read "keep current state," which had the same degenerate case as Section 3.4's note — at t=60 the current state *is* `excited`/`celebrate`, so keeping it would have retained the forbidden action and lost the demo's headline beat, making the safety net useless at the one tick that matters most. Pinning `idle_loop` matches what the persona-level fallback above already does, and `idle_loop` is disallowed by no rule in Section 4, so it is always a legal commit.
- **No retry, decided 2026-08-11 — and this is a measurement, not a preference.** A single-retry-on-failure is the obvious next feature, and it is deliberately absent. Across **50 scored runs on 2026-08-11** (500 ticks, ~2000 persona calls) with `ornith:9b`, there were **2 persona failures, 0.04 per run** — one `mood:timeout` and one `check:timeout`, no malformed output and no out-of-enum values at all. Adding retry would mean adding a code path that fires roughly once every 25 runs: barely testable against real failures, invisible in the reliability figures, and a second 120s timeout budget on a tick that already runs ~26s. Both observed failures were *timeouts*, which is the one failure mode a retry could plausibly recover — so the case is not zero, it is just far too small to justify the machinery, and the fallback it would replace already produced a legal committed state both times. **Revisit if the failure rate rises materially** — a switch to a smaller or less compliant model is the realistic trigger, since `qwen3.5:2b` and `qwen3.5:4b` were measured at 13/16 and 10/16 format compliance in Section 7.1a and would make retry worth its cost.
  - An earlier draft of this note claimed the rate was "0.00 per run" from a 20-run sample. Two failures appeared in the next 30 runs. Same lesson as 7.1q: an absence observed at n=20 is not an absence.
- **Second invariant, added 2026-08-11: never accept a rejection Section 4 does not support.** If the transition-checker returns `reject` but the action it rejected is *legal* for the committed mood and the current action, the harness restores the action-picker's proposal and records the override in `verdict.overruled`. This is the mirror image of the invariant below: one direction stops an illegal action being committed, the other stops a legal action being vetoed on an invented rule.
  - **Why this is decidable and not a matter of taste:** every rule in Section 4 forbids an **action**. `by_mood[mood]` and `by_previous_action[action]` both carry `disallowed_next_action` lists, and there is no rule kind that can forbid a mood or a line. So if the rejected action is legal, *no rule in the table could have justified rejecting it*. The harness is not overriding the checker's judgement; it is enforcing that the judgement cite the rules it was given.
  - **Why it was needed:** 7.1q measured 72% of rejections as rejections of legal actions, with fabricated reasons. None of the five acceptance criteria could detect this, because the substituted `idle_loop` is itself legal, so the invariant below stayed silent and the run still passed.
  - **Deliberately narrow.** The committed **mood** is left exactly as the checker decided. The table cannot adjudicate moods, so overruling one would be the harness inventing policy rather than enforcing the spec. Legality is evaluated against the mood being committed, so the restored action is legal by construction. The guard also stands down when the checker *failed* (`harness_fallback` — that is Section 5's last resort, not a judgement) and when the action-picker failed (there is no swarm decision to defend, only a harness default).
  - **Config:** `enforce_grounded_rejections` in `config/personas.json`, default `true`. Set `false` to measure the swarm unguarded; `config_version` distinguishes the two in the trace.
- **Invariant the harness must enforce regardless of what any persona returns:** never commit an `action` that Section 4 disallows for the committed `mood`. If a committed pair would violate the table, substitute `idle_loop` and log it. This is the deterministic backstop behind the transition-checker's model-based judgement — with it, a checker that ignores its own fallback rule degrades to a safe animation instead of an illegal state. Phase 2 should implement this in `swarm/harness.py` and Phase 2's tests should cover it (see `06_TESTING_STRATEGY.md`).

## 6. Trace log format

Every tick writes one record (JSON Lines, one per line, easy to replay/inspect):

```json
{"tick": 42, "config_version": "a1b2c3d4e5f6", "trigger": {"type": "event", "event_type": "game_danger"}, "input_state": {...}, "proposals": {"mood": {...}, "action": {...}, "line": {...}}, "errors": {"mood": null, "action": null, "line": null, "check": null}, "verdict": {...}, "final_state": {...}, "timing_ms": {"mood": 120, "action": 95, "line": 80, "check": 110}}
```
- **`trigger`** (added 2026-07-29): records why this tick fired — `{"type": "event", "event_type": "..."}` or `{"type": "timer"}`. The original schema had no way to tell, from the log alone, why a tick happened at all — needed for full explainability (PRD criterion #5) once timer-triggered ticks exist (Section 7).
- **`errors`** (added 2026-07-29): one entry per persona, `null` if it succeeded, otherwise a short reason string (e.g. `"timeout"`, `"malformed_json"`, `"out_of_enum:neutral"`). Section 5 requires fallback failures to be "logged explicitly," but the original schema had no field to hold that — without this, a reader of the trace log couldn't tell a fallback occurred at all. **Corrected 2026-07-30:** the key set is `mood`/`action`/`line`/`check` — the same four as `timing_ms`. The original example listed only the first three, but Section 5 explicitly contemplates the transition-checker failing, and that failure needs somewhere to be recorded.
- A **harness-level** fallback (Section 5's last resort, when the checker itself failed) is recorded as `errors.check` plus `verdict.verdict = "harness_fallback"`, so a reader can distinguish "the checker rejected the proposals" from "there was no usable checker verdict at all."
- **`config_version`** (added 2026-08-11): a 12-character hash of the configuration that produced this tick, computed by `swarm/version.py`. Every reliability figure in this repo is "measured under some configuration," and until now a trace file did not say which — two traces in `logs/` were indistinguishable even when a prompt clause had been added between them. That is not hypothetical: Phase 6 compared a stale trace against a fresh one and reached the **opposite** conclusion, caught only by checking file write times. The hash covers `config/personas.json`, `config/transitions.json`, and **the four prompt texts** — the prompts are included deliberately, because they are what moved reliability from 65% to 85%, and a fingerprint that ignored them would call two materially different runs identical. `_`-prefixed documentation keys are stripped first, so editing a maintainer comment does not invalidate a measurement. The event scenario is *not* hashed — it is a run input, not configuration — but it is recorded in the sidecar.
- **`verdict.overruled`** (added 2026-08-11, present only when it fired): `{"checker_action": ..., "restored_action": ..., "reason": ...}`, written when Section 5's second invariant overrules an ungrounded rejection. The verdict itself is left as the checker returned it — the log must record what the model actually said, not a tidied version — so a reader can see both the veto and the reason it was not honoured. Grep for `overruled` to count them; `scripts/run_reliability_report.py` reports the rate every run.
- Alongside each trace, the harness writes **`<trace>.meta.json`**: what the short hash expands to (models, per-persona prompt hashes, both config files, scenario, and whether personas were real or mocked). The stamp is per-record so a hand-concatenated trace stays honest; the expansion is per-file so the stamp costs 12 characters a line rather than a full config dump.

This log is what makes the system explainable (PRD success criterion #5) and is also your debugging tool during vibe-coding — when behavior looks wrong, read the trace, don't guess.

## 7. Model choice (to resolve early, before full build)

- Start with the smallest local model you already have working in your stack (per your existing local-model setup) for all four personas — same model, different prompts, to start. Only split personas onto different model sizes/families later if you find a specific persona needs more capability (e.g. transition-checker reasoning is weaker than mood-picker needs).
- Benchmark tick latency early (4 sequential calls) against your "demo is legible" bar — this affects whether ticks are event-triggered only or also timer-triggered.

**Tick triggering, decided 2026-07-30 from the 7.1a measurements:** v1 is **event-triggered, plus one idle timer at 15s** used only to fire the `idle_timeout` behaviour the Demo Script expects at t=130. Store as `tick_timer_s: 15` in `config/personas.json`.
- 15s matches the Demo Script's own event spacing, so the timer never races an event tick in the scripted run.
- **The 15s figure is a consequence of the event spacing we chose, not a constraint on model choice.** PRD Section 5 makes latency an explicit non-goal ("correctness and legibility of decisions matter more than latency for v1"), so if the model that behaves correctly needs 30s per tick, the right response is to widen the spacing in `demo_sequence.json` or let the event replay wait for the in-flight tick — **not** to pick a worse-behaving model to fit 15s. Correctness first; adapt the cadence to the model, not the model to the cadence.
- Practical note for whichever model Phase 1 picks: if a tick exceeds the spacing, event replay must **wait** for the in-flight tick rather than dropping or overlapping it, so the logical event order in the trace log stays intact. Wall-clock length can be fixed in post for the recording; a corrupted trace cannot.
- The harness must **skip a timer tick if a tick is already in flight** rather than queueing it — overlapping ticks would interleave writes to the shared state and the trace log. Phase 2 owns this.

### 7.1 Phase 0 benchmark results (measured 2026-07-29, extended 2026-07-30)

Runtime resolved: **Ollama 0.31.1** at `http://localhost:11434`, Python client `ollama` 0.6.2, Python 3.13.5.

**Critical finding — always pass `think=False`.** Several of the installed models are reasoning models. On `qwen3.5:2b`, a single trivial call took **76–159s**, emitting 6,700–13,200 characters of hidden reasoning to produce a one-sentence answer. The same call with `think=False` takes **~1.3s** — roughly a 100x difference, and the difference between a viable demo and an unusable one. `swarm/model_client.py` must pass `think=False` on every persona call (Phase 3). This is not a tuning preference; without it the project does not work.

**Superseded 2026-07-30.** The original results below covered only 3 of the 7 installed chat models and were produced ad-hoc, so they could not be re-run. They are now reproducible via `scripts/benchmark_models.py`, which measures **all four persona prompts** (not just mood-picker) and runs the two decisive t=60 probes. Extending to all 7 models **overturned the original model-choice conclusion** — see 7.1b.

### 7.1a Latency and format compliance (7 models, `think=False` + `format="json"`, warm, 4 runs each)

Tick total is the sum of the four per-persona averages, i.e. one full sequential tick per Section 1.

| Model | one tick (4 calls) | schema+enum valid | notes |
|---|---|---|---|
| `gemma4:e2b` | **6.25s** | 16/16 | fastest, perfect compliance |
| `granite4.1:3b` | 9.37s | 16/16 | |
| `qwen3.5:2b` | 9.49s | 13/16 | invents enum values (`idley`, `idle_loop_safe`) |
| `nemotron-3-nano:4b` | 12.80s | 16/16 | |
| `qwen3.5:4b` | 19.99s | 10/16 | worst: checker 0/4 valid, mostly unparseable |
| `qwen3.5:9b` | 26.59s | 16/16 | |
| `ornith:9b` | 31.82s | 16/16 | slowest |

Demo Script events are spaced ~15s apart. Only the first four models complete a tick inside that window, so **event-triggered ticks are viable for those; `qwen3.5:4b`, `qwen3.5:9b` and `ornith:9b` would still be mid-tick when the next event lands.** A fixed timer faster than ~7s is not viable with anything here.

### 7.1b The t=60 conflict: no model passes, because it is not a model problem

Two probes, both required for the Demo Script's headline beat. `mood_shift`: does mood-picker move off `excited` when `game_danger` 0.7 arrives? `legal_fallback`: fed the conflicting proposals directly, does the transition-checker reject **and** supply an action that is legal for the final mood per Section 4?

| Model | mood_shift (run 1 / run 2) | legal_fallback |
|---|---|---|
| `gemma4:e2b` | 0/4 / 0/4 | 0/4 |
| `granite4.1:3b` | 0/4 / 0/4 | 0/4 |
| `nemotron-3-nano:4b` | 2/4 / 4/4 | 0/4 |
| `ornith:9b` | 4/4 / 4/4 | 0/4 |
| `qwen3.5:2b` | 0/4 / 3/4 | 0/4 |
| `qwen3.5:4b` | 1/4 / 2/4 | 0/4 |
| `qwen3.5:9b` | 3/4 / 4/4 | 1/4 |

Three conclusions, in order of importance:

1. **`legal_fallback` fails for every model — this is a spec gap, not a model-choice problem.** The cause is documented in full in Section 3.4: models revert to `current_action`, which at t=60 *is* the forbidden `celebrate`. A controlled A/B proves the same models return a legal fallback when `current_action` is benign. **Swapping models will not fix this.** It needs the Section 3.4/5 fix, in Phase 6.

2. **`mood_shift` is probably also a prompt gap, not a capability gap.** The Section 3.5 mood prompt says nothing about danger outranking hype. At t=60 the rolling window holds two hype spikes (0.6, 0.9) and one danger (0.7), so a model that stays `excited` is not obviously wrong — the demo's *intent* that danger dominates is nowhere in the prompt. Consider this when Phase 6 opens, rather than treating the low-scoring models as unfit.

3. **The models that shift mood are the ones too slow for the 15s cadence, and the fast ones don't shift.** `ornith:9b` is the only model consistent across both runs (4/4, 4/4) but needs 31.8s/tick; `gemma4:e2b` runs a tick in 6.25s with flawless compliance but never shifts. There is no model here that is both fast and reliably mood-shifting, so expect to resolve this by fixing the prompt (point 2), widening event spacing in `demo_sequence.json`, or splitting personas across models — an option Section 7 already permits. **Do not pick a model for `config/personas.json` in Phase 1 on this evidence alone.**

**On sample size:** run 1 and run 2 are the same script on the same machine the same day. `qwen3.5:2b` moved 0/4 → 3/4 and `nemotron-3-nano:4b` 2/4 → 4/4 — the two runs would have produced opposite decisions. The original 7.1 conclusion ("only `nemotron-3-nano:4b` shifts reliably; treat as the leading candidate") was an artifact of n=4. Use n≥10 for anything that gates a decision, and report the spread. See `06_TESTING_STRATEGY.md` Section 5.

### 7.1c Re-measured after the 3.5a prompt fix (n=8, 2026-07-30)

> **Stale as of the Section 4 restructure, same day.** The `legal_fallback` column below was measured when `transitions.json` had only `by_mood` rules. Adding `by_previous_action` made the legal set at t=60 strictly smaller (`idle_loop`/`wave` only), and the mood prompt has since gained two further clauses. **Do not quote these numbers as current** — they are kept because the *direction* they establish still holds (the 3.5a fallback fix works, and per-persona capability decomposes cleanly). Re-run `scripts/benchmark_models.py` for figures that reflect the committed config.

Same two probes, re-run at n=8 immediately after the Section 3.5 prompt revisions. This is the verification that the 3.4/3.5a fix actually works, not just that it sounded right.

| Model | mood_shift | legal_fallback | verdict |
|---|---|---|---|
| `gemma4:e2b` | 0/8 | **8/8** | checker fixed, mood still won't shift |
| `granite4.1:3b` | 0/8 | **8/8** | checker fixed, mood still won't shift |
| `nemotron-3-nano:4b` | 7/8 | 4/8 | both improved, neither reliable |
| `ornith:9b` | **8/8** | **8/8** | **both pass** |
| `qwen3.5:2b` | 4/8 | 5/8 | unreliable on both |
| `qwen3.5:4b` | 7/8 | 0/8 | checker output unparseable 8/8 |
| `qwen3.5:9b` | **8/8** | **8/8** | **both pass** |

**The fallback fix is confirmed.** `legal_fallback` went from 0/4 across the board to 8/8 on four of seven models. `gemma4:e2b` moved 0/4 → 8/8 (`reject/alert/look_around`, unanimously); `granite4.1:3b` and `ornith:9b` likewise 8/8 with `idle_loop`. Adding the action enum also eliminated the invented values (`freeze`, `stay_flat`, `defensive_stance`) almost entirely — `nemotron-3-nano:4b` produced one `looking_at_map` and one malformed `retry` verdict, and `qwen3.5:4b` remains unparseable regardless.

**The mood fix helped some models and not others**, which is informative rather than disappointing: `nemotron-3-nano:4b` went ~2-4/4 → 7/8 and `qwen3.5:4b`/`ornith:9b`/`qwen3.5:9b` now shift reliably, while `gemma4:e2b` and `granite4.1:3b` stay stubbornly `excited` 0/8. The urgency line is doing real work where the model is capable of weighing it.

**Two models pass both probes: `ornith:9b` and `qwen3.5:9b`** — and both are the slowest (31.8s and 26.6s per tick from 7.1a). Per the corrected note in Section 7 above, that is acceptable: latency is an explicit PRD non-goal, so a longer tick with correct behaviour beats a fast tick that cannot produce the demo's headline beat.

**But there is a better option Phase 1 should evaluate first: split personas across models.** Section 7 already permits this and `config/personas.json` is specified as holding a model name *per persona*. The probe data decomposes cleanly:
- **mood-picker** needs a model that shifts: `ornith:9b` (8/8), `qwen3.5:9b` (8/8), `nemotron-3-nano:4b` (7/8) or `qwen3.5:4b` (7/8).
- **transition-checker** needs legal fallbacks, and the *fast* models are now perfect at it: `gemma4:e2b` (8/8, 1.69s/call), `granite4.1:3b` (8/8, 3.39s/call).
- **action-picker / dialogue-line** need only schema compliance, where `gemma4:e2b` is 16/16 and fastest.

So a plausible assignment — mood on `nemotron-3-nano:4b` (2.76s), everything else on `gemma4:e2b` (~4.7s combined) — lands near **~7.5s per tick with both behaviours covered**, which no single model achieves.

> **⚠ WITHDRAWN 2026-07-31 — this arithmetic is wrong, and the error is instructive.** Summing per-persona latencies assumes every model is already resident. It is not: Ollama unloads one model to load another, so a split across two large models pays a load penalty *twice per tick*. Measured directly — running only the checker on `ornith:9b` while the other three used `qwen3.5:9b` took **828s per run against 279s for a single model**, a 3x regression, of which roughly 600s was pure model swapping. The per-persona probes in 7.1g missed it for exactly the same reason: each model was already loaded when measured.
>
> **A per-persona split is only worth considering if the chosen models are small enough to stay co-resident.** Otherwise keep all four personas on one model. See 7.1h.

### 7.1d Phase 3 live findings (2026-07-30)

**Per-persona, in isolation at the t=60 state: all four are solid.** 10 consecutive real calls each on `qwen3.5:9b`, every one schema- and enum-valid, every `reason` populated and coherent. The transition-checker returned `reject` → `alert`/`idle_loop` — a *legal* fallback — **10/10**, confirming the Section 3.5a fix holds against a real model rather than only in the n=8 probe. A dead endpoint raises typed `ModelUnavailable`, which the harness turns into a logged fallback, not a crash. That clears Phase 3's Definition of Done.

Four full-sequence runs were then done, enabling personas one at a time per the Roadmap (`--real mood`, `--real mood,action`, `--real mood,action,line`, `--real all`). Those surfaced three things the isolated per-persona checks could not.

**(a) The t=60 conflict does NOT arise with the real action-picker — this is the Phase 4 blocker.**
Both runs including the real action-picker reported `overrides: none`. The reason is simple and was invisible until the chain ran: the *mock* action-picker deliberately modelled the "momentum" the Demo Script describes (still proposing `celebrate` while the mood turns `alert`), whereas the real action-picker simply follows the proposed mood and picks `look_around` — which is legal, so there is no conflict for the checker to catch.

Note what this says about the Phase 0 probe in 7.1c: it fed the checker a conflict **directly** (`proposed_mood=alert` + `proposed_action=celebrate`), so it proved the checker *resolves* a conflict correctly. It never tested whether a conflict *occurs*. Both facts are needed and only the first was measured.

This is exactly the case Roadmap Phase 4 task 4 anticipates — "If the conflict never actually triggers naturally, deliberately adjust `transitions.json` or the demo sequence's intensities until it does — the conflict moment must be real and reproducible, not hoped-for." Resolve it there. Options, cheapest first: raise the `chat_hype_spike` intensity at t=45 so celebratory momentum is stronger; give the action-picker explicit momentum/inertia (a prompt change, so Phase 6); or add a transition rule that bites on a pairing the action-picker actually produces.

**(b) `idle` versus `happy` is non-deterministic, so acceptance criteria 4 and 5 currently pass by luck.**
Nothing in the mood prompt says whether calm or an absence of stimulus should read as `idle` or `happy`; both are in the enum and both are defensible. Across runs, `chat_calm` at t=15 produced `idle` three times and `happy` once, and the final state landed on `idle` three times and `happy` once. Criteria 4 ("t=15 produces no change") and 5 ("run ends idle") therefore passed in most runs and failed in one, with no code change between them. **A criterion that passes 3 runs in 4 is not satisfied** — the demo has to be recorded once. Tracked as open ambiguity #5 in `06_TESTING_STRATEGY.md`; the fix is a prompt clause, so it belongs to Phase 4 or 6, not Phase 3.

**(c) Real parse and enum failures occur at a low but non-trivial rate, and the Section 5 machinery caught every one.**
Observed unprompted across the runs:
- `mood: "safe"` — the model echoed the *event type* (`game_safe`) as a mood. Caught as `out_of_enum:'safe'`, fallback applied, logged. This is precisely the case ambiguity #1 resolved in Phase 0 by ruling that valid-JSON-but-out-of-enum is a persona failure; without that ruling the harness would have committed a mood no downstream consumer understands.
- A **truncated** checker response, cut off mid-key: `{"verdict":"reject",...,"final_line"`. Caught as `parse_failure: object opened but never closed`, harness-level fallback applied, and the committed state stayed legal.
- One malformed dialogue-line response.

Roughly 2 failures per 40 calls in the all-real run. Every one was logged with a distinguishable reason and none stopped the loop — but this rate is well above Phase 6's bar of "never more than one fallback per run", so budget time there.

**Reproduce:** `python -m swarm.harness --real all`, then read `logs/trace_<ts>.jsonl`.

### 7.1e Where the acceptance criteria actually stand (measured 2026-07-30, before Phase 4)

Scored across full `--real all` runs after each change. **These are n=3–4 samples and should be read as directional only** — doc 06 Section 5 already established that n=4 cannot gate a decision on this stack.

| Change | C3 t=60 override | C4 t=15 no change | C5 ends idle | persona failures/run |
|---|---|---|---|---|
| `idle`/`happy` clause only | 1/3 | 3/3 | 2/3 | 1.0 |
| + intensity ladder | 2/4 | 2/4 | 3/4 | 2.25 |
| + `jump` smoothness rule | 3/4 | 2/4 | 1/4 | 2.5 |

**No run yet passes all four criteria at once.** C3 — the headline beat, and the thing that was structurally impossible before `by_previous_action` existed — improved from 1/3 to 3/4. C4 and C5 remain unreliable, and the totals moved in opposite directions between revisions.

**Tuning stopped here deliberately.** Successive revisions began trading criteria against each other (C4 fell from 3/3 to 2/4 while C3 rose), and at three or four samples those swings are indistinguishable from noise — which is exactly the trap doc 06 Section 5 documents and the Roadmap time-boxes to Phase 6. Continuing would have been fitting prompt wording to sampling error.

**What Phase 4 should do first, in order:**
1. **Build `scripts/run_reliability_report.py` (doc 06 Section 5) before changing anything else.** Every remaining question here is "is this rate really different?", and n≥10 is the minimum that can answer it. Re-establish a baseline at n=10 for all four criteria plus the failure rate.
2. Only then adjust — and adjust **one thing at a time**, re-running the full report each time. Phase 4 task 4 sanctions changing `transitions.json` or the demo sequence's intensities; **the event intensities have deliberately not been touched**, so that lever is still fully available and is the natural next one, since it does not risk the cross-criterion interference that prompt edits caused here.
3. The persona failure rate (1.0–2.5 per run, 10 across the last 4 runs) is well above Phase 6's "never more than one fallback per run" bar and needs its own look; it is mostly `parse_failure` on the checker and line personas.

### 7.1f Phase 4 baseline — n=10, all four personas real (2026-07-30)

Produced by `scripts/run_reliability_report.py --runs 10 --real all`; artifact at `tests/reliability/phase4_baseline.json`.

| Criterion | Pass rate |
|---|---|
| C1 run completes without crashing | **10/10** |
| C2 3+ distinct moods and 3+ distinct actions | **10/10** |
| C3 genuine checker override at t=60 | 7/10 |
| C4 `chat_calm` at t=15 produces no change | 8/10 |
| C5 run ends in a stable idle state | 7/10 |
| **All five in the same run** | **4/10** |

Persona failures 1.60/run. Mean wall time 325s per run (~5.4 min), so a full 10-run report is roughly 55 minutes.

**Roadmap Phase 4's Definition of Done is met.** Its four boxes are phrased per-run, and runs 1, 7, 8 and 10 satisfy all of them: four personas real with no mocks, an identifiable t=60 override, 3+ moods and actions, and no change at t=15. Task 4's conditional ("if the conflict never actually triggers naturally…") does not apply — it triggers naturally in 7 of 10 runs, so `transitions.json` and the demo sequence were left alone.

**The most important finding is qualitative, and it came from reading the t=60 line by hand.** The transition-checker reliably *detects* the conflict and unreliably *resolves* it. A representative tick (`tests/fixtures/sample_trace_real.jsonl`, tick 4):

> proposed: mood `alert`, action `look_around`, from a committed `excited`/`celebrate`
> checker: `reject`, `final_action: "jump"` — *"Alert mood allows jump. Previous action 'celebrate' disallows look_around."*
> committed: `alert`/`idle_loop`, with `errors.check` recording the substitution

The diagnosis is exactly right and the stated premise is exactly wrong: `by_mood.alert` disallows `jump`. This is the `reject`/`jump` false pass predicted in Section 4's notes during Phase 0, now observed with a real model — and the Section 5 invariant caught it, as it was built to. Across the runs examined the checker names an illegal fallback roughly 20–30% of the time, and the invariant caught **every** one; no illegal state was ever committed in 10 runs.

That is a good defence-in-depth story, but it changes what the Phase 7 writeup may honestly claim. In those ticks the *swarm* did not resolve the conflict — a deterministic rule did. The report therefore prints both numbers: C3 (a genuine checker resolution, the demo's narrative beat) and "intervened at all (checker OR harness invariant)" (the safety property).

**Not carried into Phase 4, deliberately:** the 8/10 all-criteria rate and the ≤1/run failure rate are **Phase 6's** Definition of Done, not Phase 4's. Chasing them here would be phase-jumping.

### 7.1g Phase 6 — the transition-checker gets its own model (2026-07-31)

Phase 6 cycle 2 left C3 at 6/10, and categorising the four misses showed the cause was not wording:

| Run | What happened | Cause |
|---|---|---|
| 1 | `approve celebrate → look_around` | checker approved an illegal transition |
| 5 | `harness_fallback` | checker call failed |
| 7 | `approve wave → look_around` | **no conflict arose** — `wave` has no smoothness rule, so this is legitimate |
| 9 | `reject` then named `look_around` | checker rejected correctly, then named the action it had just rejected |

Three of four are the checker; a perfect checker would put C3 near 9/10, since run 7's miss is structural rather than a fault. Three prompt revisions across Phases 3–6 never moved this class of error, so the next lever was the one Section 7 already sanctions — *"split personas onto different model sizes/families later if you find a specific persona needs more capability."*

Each installed model was probed on the **exact failing tick** (committed `excited`/`celebrate`, proposed `alert`/`look_around`; only `idle_loop` and `wave` are legal), n=8:

| Model | Correct | s/call | Behaviour |
|---|---|---|---|
| **`ornith:9b`** | **8/8** | 13.6 | `reject`/`idle_loop` every time |
| `qwen3.5:9b` (incumbent) | 5/8 | 12.3 | 3× named the rejected action |
| `gemma4:e2b` | 4/8 | 3.8 | 3× named the rejected action, 1× approved it |
| `nemotron-3-nano:4b` | 2/8 | 5.6 | mostly named the rejected action |
| `granite4.1:3b` | 0/8 | 5.2 | approved the illegal transition 8/8 |

On this evidence the checker was moved to `ornith:9b` while the other three stayed on `qwen3.5:9b`, at an apparent cost of ~1.3s on one call in four. **That split was then measured end to end and reverted — see 7.1h.** The per-call figure above is real but misleading: it was taken with the model already resident, and in a mixed run the swap dominates. The committed config runs **all four personas on `ornith:9b`** (7.1i).

**A finding worth keeping:** `granite4.1:3b` scored **8/8 under the old single-rule table and 0/8 under two rules**. The smoothness rule is materially harder than the pairing rule — it requires reasoning about the *previous* state rather than reading a lookup keyed on the proposal — and the smaller models do not carry it. Any future model substitution must be re-probed against the two-rule table; Section 7.1c's figures cannot be reused.

### 7.1h The split was measured and rejected — model-swap thrashing (2026-07-31)

Running the checker on `ornith:9b` with the other three on `qwen3.5:9b` was measured at n=10 and **reverted**:

| | all `qwen3.5:9b` | split checker | verdict |
|---|---|---|---|
| C3 t=60 override | 6/10 | 7/10 | within noise |
| Runs passing all criteria | 4/10 | 3/10 | no gain |
| **Wall time per run** | **279s** | **828s** | **3x worse** |

The checker itself did improve — its own judgement errors at t=60 fell from 3 to 1, exactly as 7.1g's probe predicted. But the split costs a **3x latency regression**, because Ollama holds one model at a time: each tick makes three calls on one ~6GB model, one on another, and pays an unload/reload twice. The arithmetic accounts for the whole gap (~230s inference + ~600s swapping).

**The lesson is about how the probe was run, not about the models.** Every per-persona measurement in 7.1c and 7.1g was taken with the model already resident, so none of them could have surfaced this. A benchmark that measures components in isolation cannot predict the cost of composing them — the same mistake, in a different guise, as the Phase 0 probe that fed the checker a conflict directly and so proved it could *resolve* one but never that one *occurs*.

This also breaks Phase 7 independently of the criteria: a 14-minute run cannot be screen-recorded as the 60–90 second demo the Demo Script asks for.

**Conclusion: keep all four personas on a single model** unless a future split uses models small enough to remain co-resident. `ornith:9b` was strongest on both failing dimensions (checker 8/8 in 7.1g; mood-shift 8/8 in 7.1b), so the remaining question is whether running *everything* on it beats everything on `qwen3.5:9b` — a single-model change with no swap penalty.

### 7.1i Phase 6 result — all four personas on `ornith:9b` (n=10, 2026-07-31)

Adopted. `config/personas.json` now runs every persona on `ornith:9b`.

| Criterion | qwen baseline | qwen cycle 2 | split | **all `ornith:9b`** |
|---|---|---|---|---|
| C1 completes | 10/10 | 10/10 | 10/10 | **10/10** |
| C2 3+ moods and actions | 10/10 | 10/10 | 9/10 | 8/10 |
| C3 t=60 override | 7/10 | 6/10 | 7/10 | **10/10** |
| C4 t=15 no change | 8/10 | 7/10 | 8/10 | 7/10 |
| C5 ends idle | 7/10 | 9/10 | 7/10 | 9/10 |
| **All five together** | 4/10 | 4/10 | 3/10 | **5/10** |
| **Persona failures/run** | 1.60 | 0.40 | 0.70 | **0.00** |
| Wall time/run | 325s | 279s | 828s | 398s |

**Phase 6's second Definition-of-Done item is met**: zero persona failures across all 10 runs, and the last three runs had 0 each, comfortably inside "never more than one fallback per run in your last 3 test runs". The extractor repair plus a model that reliably emits well-formed JSON removed the failure class entirely.

**Phase 6's first item is not met**: 5/10 runs pass all five criteria against a bar of 8/10. C3 — the demo's headline beat — is now **perfect at 10/10**, having been 7/10 at baseline. The residual gap is C4 and C2.

**C4 (7/10) is a stable, identified failure with no cheap fix.** All three misses are byte-identical in shape: `chat_calm` at t=15 → `happy`/`wave`. It has sat at 7–8/10 across every configuration tried, and the one prompt clause aimed squarely at it (Section 3.5a, third revision) failed to move it while costing C2 and C5. The model reads a calm-chat event as mildly positive, which is a defensible reading the Demo Script simply does not share.

**C2 (8/10) is a genuine design tension, not a defect — and it is the interesting one.** Both misses show 4 distinct moods but only **2 distinct actions** (`celebrate`, `idle_loop`). The cause is C3's own success: the checker now overrides at t=60 in every run and, correctly, falls back to `idle_loop` — the only obviously-safe legal action. The more reliably the arbiter intervenes, the more the character converges on the fallback pose, and action variety collapses. **Maximising C3 works against C2.**

**⚠ The paragraph above was wrong and is corrected in 7.1k. C2 and C3 are not in tension.** Measurement showed the action-picker produces full variety on its own (`wave` 8/8 for `happy`, `look_around` 7/8 for `alert`), and one overridden tick in ten cannot account for a run spent in `idle_loop`. The real cause was the transition-checker over-rejecting proposals that break no rule. Kept here because the wrong hypothesis is instructive: it was plausible, self-consistent, and would have led to weakening a correct rule to "fix" something that rule was not causing.

### 7.1j C4 was a conflict between two documents, not a model weakness (2026-07-31)

C4 (`chat_calm` at t=15 must produce no change) sat at 7–8/10 across four different model configurations and survived three prompt revisions aimed at it. The cause was not the model:

- **Interface Contract 2.1** set presence-only signals to `intensity: 1.0`, meaning "this signal is fully present" — `chat_calm` at 1.0 means chat *is* calm. It explicitly rejected `0.0` as inverting ("calm at 0.0" reads as *not* calm). Locally sensible.
- **Architecture 3.5** reads intensity as *strength*: "a strong one (intensity 0.8 or above) is excited". Also locally sensible.

Together they meant the event representing *nothing is happening* arrived as the strongest possible signal. Varying only that number, on the t=15 tick:

| `chat_calm` intensity | mood = `idle` |
|---|---|
| 1.0 | 6/8 |
| 0.5 | 4/8 |
| **0.1** | **8/8** |
| 0.0 | 8/8 |

Set to `0.1` — low enough to remove the false strength signal, non-zero so the original inversion objection still holds. Result at n=10: **C4 7/10 → 10/10, and C5 9/10 → 10/10**, lifting runs-passing-everything from 5/10 to 7/10.

**Worth remembering as a class of defect:** neither document was wrong on its own terms, and no review of either in isolation would have caught it. Only the number's journey between them was wrong. Three prompt revisions failed because they were arguing against a value in the payload.

### 7.1k C2's real cause: the checker over-rejects legal proposals (2026-07-31)

With C4 fixed, C2 became the binding constraint at 8/10. Action coverage across 10 runs:

| Action | Appears in |
|---|---|
| `idle_loop` | 10/10 |
| `wave` | 9/10 |
| `celebrate` | 8/10 |
| `look_around` | 5/10 |
| `duck` | **0/10** |

The two missing actions are exactly the two the pairing guide assigns to `alert`. Probing the checker with proposals that break **no** rule:

| Case (all legal) | Approved |
|---|---|
| `alert` + `look_around` | 0/8 |
| `alert` + `duck` | 0/8 |
| `happy` + `wave` | 8/8 |
| `idle` + `idle_loop` | 8/8 |

`alert` is the one mood *named* in the rules table, and the checker over-generalises from "this mood is mentioned" to "be cautious, substitute `idle_loop`". The rules are a **denylist**, and Section 3.5 never said so. Added: *"These lists are the ONLY grounds for rejection… do not treat a mood as restricted just because it is named above."*

**Measured, then REVERTED.** In isolation the clause did what it was designed to: over-rejection improved from 0/8 to 3/8 on both `alert` cases, and the legitimate t=60 rejection was unharmed (still 8/8 on the probe). Over a full n=10 run it was a net loss:

| | without the clause | with it |
|---|---|---|
| C2 3+ moods and actions | 8/10 | **10/10** |
| C3 t=60 override | 9/10 | 7/10 |
| C5 ends idle | 10/10 | 8/10 |
| **Runs passing everything** | **7/10** | **5/10** |

C2 was fixed exactly as intended — `duck` finally appears. But telling the model "reject only when the rule applies" made it more permissive in *both* directions: one run shows it **approving** `celebrate → duck`, which `by_previous_action.celebrate` explicitly forbids. Trading two genuine rejections for two spurious ones is a bad deal when the genuine one is the demo's headline beat.

**The lesson is about the probe again.** The isolated probe measured only the false-positive direction (does it wrongly reject a legal proposal?) and never the false-negative one (does it now wrongly approve an illegal one?). A one-sided probe will endorse any change that trades one error type for the other. Probe both directions, or measure end to end.

The `alert`/caution association therefore stands unfixed, and C2 stays at 8/10. Leaving it there is the right call unless someone finds a change that improves C2 **without** loosening rejection.

**A structural option: tried, measured, and reverted.** `by_mood.alert` is redundant for the t=60 beat — since Section 4 gained `by_previous_action`, the conflict fires entirely through `by_previous_action.celebrate`, and `by_mood.alert` never applies to a proposal the action-picker actually makes. The hypothesis was that the checker over-generalises *because `alert` is the one mood named in the table*, so deleting the entry would stop it and fix C2.

**The hypothesis was wrong.** With the entry removed and nothing else changed:

| Case (legal in both configurations) | with `by_mood.alert` | without it |
|---|---|---|
| `alert` + `look_around` approved | 0/8 | **0/8** |
| `alert` + `duck` approved | 0/8 | 1/8 |

Identical. The checker's caution around `alert` has nothing to do with the mood appearing in the rules table — it is an association with the *word*, and no table edit reaches it. Removing the rule therefore bought nothing while discarding a correct constraint, so it was restored.

The risk direction was probed too, and was clean: with `celebrate` and `jump` newly legal at t=60, the checker still chose `idle_loop` 8/8 rather than "resolving" the danger by continuing to celebrate. Worth knowing, but irrelevant given there was no upside to weigh it against.

**What this rules out.** C2's residual 8/10 is not fixable through `transitions.json`, and the one prompt clause aimed at it (above) was a net loss end to end.

### 7.1l No model approves a legal `alert` action — the over-rejection is universal (2026-07-31)

The last remaining lever was the checker model. `ornith:9b` had been selected on **one** criterion — does it reject the illegal t=60 proposal — and nothing had tested whether it correctly *approves*. All seven installed chat models were scored on both directions, n=8 per case:

| Model | REJECT t=60 | `alert`+`look_around` | `alert`+`duck` | `happy`+`wave` | s/call |
|---|---|---|---|---|---|
| `gemma4:e2b` | 8/8 | 0/8 | 0/8 | 0/8 | 2.1 |
| `granite4.1:3b` | 1/8 | 1/8 | 0/8 | 0/8 | 3.5 |
| `qwen3.5:2b` | 4/8 | 0/8 | 0/8 | 0/8 | 3.4 |
| `nemotron-3-nano:4b` | 3/8 | 0/8 | 0/8 | 0/8 | 5.0 |
| `qwen3.5:4b` | 6/8 | 1/8 | 0/8 | 7/8 | 7.0 |
| `qwen3.5:9b` | 8/8 | 0/8 | 0/8 | 6/8 | 11.0 |
| **`ornith:9b`** | **8/8** | 0/8 | 0/8 | **8/8** | 9.7 |

**Not one model will approve `alert` + `look_around` or `alert` + `duck`**, even though neither breaks any rule and the pairing guide names both as the actions for `alert`. The behaviour is universal across a 2B–9B range and three model families, so it is a property of the task rather than of any model, and **C2 is not reachable by model substitution**.

Two side findings worth keeping:
- **`ornith:9b` is confirmed the right checker.** It is the only model perfect on both the legitimate rejection (8/8) and the `happy` control (8/8). Nothing better is installed.
- **`gemma4:e2b` is a universal rejecter** — 8/8 on the reject case and 0/8 on all three approve cases. Its strong showing in 7.1g was indiscriminate refusal, not judgement. A single-direction probe scored it as a serious candidate; scoring both directions exposes it. This is the clearest illustration yet of why one-sided probes mislead.

**Accepted.** C2 stays at 8/10 and the shipped configuration stands. Every available lever has now been measured and rejected on evidence: the rules table (7.1k), the checker prompt (7.1k), and the model (here).

> **Partially overturned 2026-08-11 — see 7.1q.** The checker-prompt lever was not exhausted, only one *kind* of clause was. 7.1k tried an anti-over-rejection nudge (a threshold change); stating instead that the rules table is **exhaustive** — a missing rule — doubled approval of legal `alert` actions and put `alert` + `look_around` into the committed state, which this section recorded as 0/8 universally. The over-rejection is therefore **not** purely "a property of the task": a measurable share of it was a prompt gap. The conclusion that model substitution cannot fix it still stands.

**One untested hypothesis, recorded rather than pursued.** The checker receives the full shared state including `recent_events`, so at t=75 it can see the `game_danger` events directly — it may be over-indexing on *danger being present* rather than on the rules it was asked to apply. Section 3.4 specifies that input, so narrowing it would be a deliberate spec change; it is the one lever nobody has tried.

### 7.1m Aggregate over 20 runs — superseded by 7.1p (2026-08-01)

A second independent n=10 run on the committed configuration, for a total of 20:

| Criterion | Measurement A | Measurement B | **Aggregate** |
|---|---|---|---|
| C1 completes | 10/10 | 10/10 | **20/20 (100%)** |
| C2 3+ moods and actions | 8/10 | 10/10 | **18/20 (90%)** |
| C3 t=60 override | 9/10 | 9/10 | **18/20 (90%)** |
| C4 t=15 no change | 10/10 | 10/10 | **20/20 (100%)** |
| C5 ends idle | 10/10 | 7/10 | **17/20 (85%)** |
| **All five together** | 7/10 | 6/10 | **13/20 (65%)** |
| Persona failures | 0.00/run | 0.00/run | **0.00/run** |

**Nothing changed between the two measurements** — the config, prompts and model are identical. C2 and C5 simply traded which one failed. That is worth stating plainly because it invalidates a framing used repeatedly earlier in this document: there is no single blocking criterion. Every criterion individually sits at 85–100%; the shortfall is that they do not all land in the same run.

This explains the pattern in 7.1e, where successive changes appeared to trade criteria against each other. They partly were — but part of what looked like a trade was two noisy criteria being sampled twice. **A conclusion about "which criterion is the problem" needs more than one n=10 run**, and several conclusions earlier in this document were drawn from exactly one.

**Phase 6's two Definition-of-Done items, as they stood at this point:**
- Persona failure rate ≤1 per run — **met**, at 0.00 across all 20 runs.
- 8/10 runs passing every Functional criterion — **not met**, at 13/20 (65%).

> **Superseded 2026-08-09 — both items are now met.** Phase 8's second scenario exposed a missing rule about threat ordering; supplying it took this to **17/20 (85%)** on a fresh 20-run baseline. See 7.1p. The figures in this section are the pre-fix state and are kept for the comparison, not as the current result.

### 7.1n Phase 8 — a second scenario, and what it exposed (2026-08-09)

Roadmap Phase 8 asks for "a second demo scenario/event sequence to show the system generalizes beyond the one scripted run". `events/alt_sequence.json` is that scenario, deliberately built to stress different paths: it **opens hot** rather than idle, runs **two separate danger episodes**, includes a **mild** hype spike (0.5) to test whether the intensity ladder distinguishes `happy` from `excited`, and brings its second danger out of `wave` rather than `celebrate` — so it exercises the `by_mood` rule rather than the `by_previous_action` smoothness rule that fires in the canonical run.

**It does not generalise well, and that is the finding.** One real run (`demo/trace_alt_scenario.jsonl`):

| | Canonical | Alt scenario |
|---|---|---|
| Distinct moods | 3–4 | 3 (`excited`, `alert`, `angry`) |
| Distinct actions | 3–4 | **2** (`idle_loop`, `look_around`) |
| Returns to idle | yes | **no** — ends `alert` after 11 ticks |
| Overrides | 1–2 | 6 |

Two distinct problems, both invisible in the canonical sequence:

1. **The character never recovers.** From tick 2 onward it holds `alert` or `angry` through two `game_safe` events and a hype spike. This is the Section 3.5 urgency clause ("threat or danger signals take precedence") interacting with the 60-second event window: while *any* danger remains in the window the mood stays elevated, and the alt scenario's later danger (t=85) keeps one in scope almost to the end. The canonical sequence recovers only because its dangers (t=60, 75) age out before the trailing timer ticks. **The recovery behaviour the demo shows is partly an artifact of that one event schedule.** That is exactly what a second scenario is for.
2. **`game_safe` produced `angry`** at tick 4 — the opposite of the intended reading. Consistent with the same over-weighting of the danger history.

**A real bug fell out of it too.** Tick 4's dialogue line was the literal four-character string `"null"`. The model was asked for "a short line or null" and wrote the word; it is a valid string, so it passed every check and would have been drawn as a speech bubble reading "null". Now normalised to real silence in `validate_proposal`, with tests covering the near-misses (`null`, `none`, `nil`, `n/a`, empty) and a guard that a genuine line beginning with those words is not swallowed. **No test caught this** — the canonical run simply never triggered it, which is the argument for having a second scenario at all.

**Both were subsequently fixed — see 7.1p.** They were initially left alone on the grounds that Phase 6 was closed and that touching the urgency clause would invalidate the published baseline. The second of those was true, and the baseline was duly re-earned; the first turned out to be the wrong instinct. The alt scenario had found a genuine specification gap, not a tuning preference, and fixing it improved the canonical run as well.

### 7.1o Phase 8 — parallelising persona calls is blocked by the architecture, not by effort

The prize is real and worth stating: measured on the canonical trace, a tick is **33.2s** as the sum of four sequential calls, against **14.3s** for the slowest single call — a **57% ceiling** if the four ran concurrently.

It is not reachable without redesigning the negotiation. The chain has **zero available concurrency**:

- action-picker needs `proposed_mood` (Section 3.2)
- dialogue-line needs both mood and action (Section 3.3)
- transition-checker needs all three (Section 3.4)

Every call depends on its predecessor's output, so no pair can overlap. Extracting the 57% means each persona proposing blind and reconciling afterwards — a different negotiation model, and precisely what Section 1 defers: *"Parallelizing is a valid v2 optimization but adds negotiation complexity not needed for the demo."*

It also would not buy what it appears to. PRD Section 5 makes latency an explicit non-goal, and the pacing problem it would address is already solved by rendering from the trace rather than in real time (7.1h). **Deferred deliberately, with the number attached** so the next person decides on evidence rather than assumption.

### 7.1p The recovery clause — the alt scenario's fix, which also cleared Phase 6's bar (2026-08-09)

7.1n reported that the swarm never recovered in the alt scenario. The cause was a **specification gap, not a model weakness**, and the model's own reasons gave it away. At a tick whose most recent event was `game_safe`, it answered:

> *"Recent danger event overrides earlier positive events"*

— but the `game_safe` at t=105 arrived **after** the danger at t=85. It had the ordering backwards. Section 3.5's urgency clause said danger outranks social and hype signals; it never said anything about ordering **between threat signals**, so a danger anywhere in the 60-second window kept the mood pinned indefinitely. The model was following the instruction correctly. The instruction was incomplete.

One clause was added to the mood prompt:

> *Threat signals are ordered among themselves. An all-clear that arrives after a danger supersedes it: if the most recent threat-related signal says the danger has passed, treat the threat as over and let the mood recover, however many earlier dangers are still in view.*

**Result on the alt scenario:** recovery works. `alert → excited` at the first all-clear, and the run ends on `idle` — against nine straight ticks stuck in `alert`/`angry` before.

**Result on the canonical sequence — the bar is met.** Two fresh independent n=10 measurements:

| Criterion | Before (20 runs) | After (20 runs) |
|---|---|---|
| C1 completes | 20/20 | 20/20 |
| C2 3+ moods and actions | 18/20 | 18/20 |
| C3 t=60 override | 18/20 | **19/20** |
| C4 t=15 no change | 20/20 | 20/20 |
| C5 ends idle | 17/20 | **19/20** |
| **All five together** | **13/20 (65%)** | **17/20 (85%)** |
| Persona failures | 0.00/run | 0.00/run |

**Phase 6's Definition of Done is now met on both items** — 85% against an 80% bar, with both n=10 samples individually at 9/10 and 8/10, and zero persona failures.

> **The 85% figure is withdrawn (2026-08-11) — see 7.1q.** It was a real measurement, but three later n=10 samples of one *unchanged* configuration returned 6, 9 and 8: one distribution sampled three times, not three results. Separating 85% from 75% at 80% power needs ~250 runs per arm. **The diagnosis in this section still stands** — the recovery clause fixed a real specification gap, and the model's own logged reasons prove it — but the *size* of the improvement was never measurable at n=20, and neither were the effect sizes anywhere else in 7.1. The current figure is **19/20 (95%)** on config `d3f620203e0a`, itself reported with an interval rather than as a point.

**On the risk this ran, and how it was checked cheaply.** One early alt run produced `game_danger → happy`, which would have been fatal: C3 depends on the mood turning `alert` at t=60. Rather than conclude from n=1 or spend ~55 minutes on a full re-baseline to find out, the single decisive tick was probed directly — the t=60 state, mood-picker only, n=10. It came back **`alert` 10/10**, identical to the pre-clause baseline, with every response citing the urgency rule. The anomaly was a different state and was noise. That probe cost two minutes and correctly predicted the full result.

**Why this one worked where three earlier prompt revisions did not.** Each earlier attempt adjusted a *preference* — how eagerly to read calm as happy, how strictly to reject. Those traded one criterion for another because they moved a threshold. This one supplied a **missing rule** about ordering, which nothing else depended on, so it improved C5 and C3 without costing anything. The distinction is worth keeping: a clause that states something the spec assumed is safe; a clause that shifts a threshold is a trade.

### 7.1q Most of the checker's rejections were never backed by the rules table (2026-08-11)

A maturity review asked a question none of the five criteria can answer: **when the checker rejects a proposal, is the rejection actually justified by Section 4's table?** The criteria cannot see this. A rejection of a *legal* action still commits `idle_loop`, which is legal, so the Section 5 invariant stays silent and every criterion can still pass. The system degrades safely and wrongly at the same time.

Measured across a 20-run baseline, by re-deriving `banned_actions(mood, current_action)` for every rejection:

| | |
|---|---|
| Rejections of an action the table forbids | 20/71 |
| **Rejections of a perfectly legal action** | **51/71 (72%)** |
| Runs containing at least one | **19/20** |

The stated reasons are confabulations, not misreadings:

> *"Wave is disallowed when mood is excited due to alert mood constraint"* — the mood was `excited`; it imported `alert`'s rule.
> *"look_around disallowed after excited mood per rules"* — `excited` has no `by_mood` entry at all.
> *"Duck disallowed after idle_loop"* — `by_previous_action` has entries only for `celebrate` and `jump`.

The model pattern-matches the *shape* of a prohibition table and invents plausible-sounding rules. This is the root cause of the action-variety weakness that 7.1l accepted as unfixable: runs collapse to `{celebrate, idle_loop}` because legal `wave` and `look_around` keep being refused.

**The cause was another missing rule.** The checker prompt explained how to reject but never stated that the table is *exhaustive*. One clause was added to Section 3.5:

> *The table is exhaustive: it lists every restriction that exists. A mood with no by_mood entry restricts nothing, and an action with no by_previous_action entry may be followed by anything. Approve unless a rule above literally names the proposed action - never infer a restriction because one seems plausible for the mood.*

**Result — a real but partial fix.** Genuine rejections held constant at exactly 1.0/run (the t=60 beat), while fabricated ones fell:

| | Before | After |
|---|---|---|
| Rejections per run | 3.55 | 2.90 |
| **Fabricated rejections per run** | **2.55** | **1.90** (−25%) |
| Legal `alert` actions approved | 11/57 (19%) | **11/29 (38%)** |
| Mean distinct actions per run | 3.0 | 3.3 |

**This partially overturns 7.1l.** That section concluded the over-rejection of legal `alert` actions was "a property of the task rather than of any model" and that "every available lever has been measured and rejected on evidence: the rules table, the checker prompt, and the model." The checker-prompt lever had been tried in 7.1k with an *anti-over-rejection* clause, which was a threshold nudge and duly failed. The exhaustiveness clause is a different kind of change — a missing rule, the category 7.1p identifies as safe — and it doubles the approval rate. **`alert` + `look_around` now reaches the committed state**, which 7.1l recorded as 0/8 for every installed model. The dead end was a prompt gap, not a capability limit.

**Final figures, pooled over 30 runs on the shipped config** (`8e742615d330`, samples of 6/10, 9/10, 8/10):

| | Baseline (20 runs) | Shipped (30 runs) |
|---|---|---|
| Ungrounded rejections | 51/71 (72%) | **48/78 (62%)** |
| Runs passing all five criteria | 14/20 (70%) | 23/30 (77%), CI [59%, 88%] |
| C2 3+ moods and actions | 16/20 | 28/30 |
| C5 settles to idle | 19/20 | 27/30 |
| Harness invariant fires | 0 | 0 |

**A caution on all of the above.** The three n=10 samples of this one unchanged configuration scored 6, 9 and 8. Differences of the size being discussed here — 72% vs 62%, 70% vs 77% — are barely larger than the sampling noise, and separating 85% from 75% at 80% power would need ~250 runs per arm. The *diagnosis* in this section is solid (the confabulated reasons are verbatim from the traces, and the mechanism is checkable tick by tick). The *effect sizes* are not measured to the precision they appear to have. Every earlier section of 7.1 has the same weakness and should be read the same way.

**Prompting did not finish the job, so the second invariant was built** (decided by the project owner 2026-08-11; specified in Section 5). The checker still fabricated ~1.9 rules per run after two prompt attempts, so the harness now refuses to honour a rejection whose action the table permits — the mirror of the rule it already applied to illegal commits. The argument that this is *enforcement* rather than the harness overriding the swarm is given in Section 5: every rule in Section 4 forbids an action, so a rejection of a legal action cites nothing.

**Measured over 20 runs on the guarded config `d3f620203e0a`:**

| | Guard off (30 runs) | Guard on (20 runs) |
|---|---|---|
| Ungrounded rejections | 48/78 (62%) — all honoured | 24/46 (52%) — **all 24 overruled, 0 honoured** |
| Guard fired at the t=60 conflict | n/a | **0 times** |
| Distinct actions per run (mean) | 3.43 | **3.90** |
| Runs with fewer than 3 distinct actions | 2/30 | **0/20** |
| C2 3+ moods and actions | 28/30 | **20/20** |
| C3 t=60 override | 28/30 | **20/20** |
| All five criteria | 23/30 (77%) | **19/20 (95%)** |
| Persona failures | 0.03/run | 0.00/run |

**How much of that to believe.** The pass-rate difference is **not** statistically established: Fisher exact on 19/20 vs 23/30 gives p = 0.12, and 7.1p's lesson about n=10 applies here as forcefully as anywhere. What *is* established is the mechanism, because it is deterministic: 24 of 24 ungrounded rejections were overruled and none slipped through, verifiable tick by tick in the traces. The action-variety gain has a clear causal path — legal `wave` and `look_around` now reach the committed state instead of being vetoed — and it is the direct cause of C2 reaching 20/20. Prefer the mechanism you can check over the rate you cannot.

**The guard does not fix the checker, and the reporting keeps them separate.** Tick 6 of the canonical trace is a model that writes *"no rule violation found actually - happy and wave are both valid. Let me recheck."* and returns `reject` regardless: the verdict and the reasoning have come apart. `scripts/run_reliability_report.py` therefore keeps printing the ungrounded-rejection rate as a measure of the **model**, with the overruled count on a separate line. Folding them together would remove the only signal that would show whether the underlying behaviour ever improves.

**Now measured every run.** `scripts/run_reliability_report.py` reports this as a standing statistic, so it can never again be invisible. Mocked personas score 0%, which confirms the metric discriminates rather than flagging everything.

### 7.2 JSON enforcement decision (resolves Section 3.6)
Use `format="json"` (Ollama's built-in JSON mode — the "stricter" option in Section 3.6) from the start: it costs one kwarg and clearly helps. **But it is not a guarantee, and the original "12/12 across all three models" claim did not survive a wider sample.** With JSON mode enabled, `qwen3.5:4b` still returned unparseable checker output 3/4, and `qwen3.5:2b` freely emitted out-of-enum values (`idley`, `freeze`, `stay_flat`, `flee_escape_danger_area`) and once an empty `final_action`. So keep **both** layers, and treat them as load-bearing rather than belt-and-braces: `format="json"` guarantees at best *parseable JSON*, never *correct keys or in-enum values*. The permissive regex extractor and the enum check required by Section 5 are what actually hold the line.

## 8. Repo structure (lock this before coding — prevents every vibe-coding session from inventing its own layout)

```
pixel-swarm/
├── README.md                     # short project summary + how to run the demo
├── LICENSE                       # MIT — added 2026-08-11 before the public launch
├── .github/workflows/tests.yml   # CI: the suite on clean Linux, no model needed
├── docs/                         # the six planning docs, moved off the root 2026-08-11
│   ├── 01_PRD.md                 #   so the front page reads as a project, not a
│   ├── 02_ARCHITECTURE_HARNESS_SPEC.md   #   filing cabinet. Three of these are
│   ├── 03_COMPILER_INTERFACE_CONTRACT.md #   PARSED by the validators as the source
│   ├── 04_DEMO_SCRIPT_ACCEPTANCE_CRITERIA.md  # of truth for enums, prompts and the
│   ├── 05_IMPLEMENTATION_ROADMAP.md      #   demo table — they are load-bearing code
│   └── 06_TESTING_STRATEGY.md            #   inputs, not just reading material
├── config/
│   ├── personas.json             # enum lists for mood/action, model name per persona
│   └── transitions.json          # disallowed-transition rules (Section 4)
├── swarm/
│   ├── harness.py                # the tick loop (Section 1) + trace logging (Section 6)
│   ├── personas.py                # one function per persona, each wrapping a model call
│   ├── state.py                   # shared state object (Section 2) as a small dataclass
│   └── model_client.py            # thin wrapper around whatever local inference runtime you use
├── events/
│   ├── demo_sequence.json         # the scripted event list (Demo Script doc, Section 1)
│   └── alt_sequence.json          # Phase 8 — a second scenario, to show the swarm is not tuned to one script
├── pixel_world/                     # added 2026-08-01 — the renderer (see note below)
│   ├── __init__.py
│   └── renderer.py                 # pixel-art renderer; knows NOTHING about the swarm
├── compiler_adapter/
│   └── adapter.py                  # translates Directive -> pixel_world calls (Interface Contract doc)
├── scripts/                        # standalone one-off scripts (added 2026-07-29 — referenced by Roadmap Phases 0/1/6 but missing from this tree originally)
│   ├── smoke_test_model.py         # Phase 0 — raw model runtime connectivity check
│   ├── validate_configs.py         # Phase 1 — config/event JSON validity + cross-references
│   ├── validate_repo_structure.py  # Phase 0 — diffs actual tree against this section
│   ├── benchmark_models.py         # Phase 0 — added 2026-07-30; reproducible version of the Section 7.1 measurement
│   ├── validate_prompt_fidelity.py # added 2026-07-30 — asserts code prompts match Section 3.5 verbatim
│   ├── run_reliability_report.py   # Phase 4 — scores every Functional criterion over N runs (Testing Strategy doc, Section 5)
│   └── run_reliability_report.py   # Phase 4/6 — N-run aggregate pass rate (Testing Strategy doc, Section 5)
├── requirements.txt                 # added 2026-07-29 — the one runtime dependency (`ollama`) Section 9 already calls for; nothing else pins it
├── tests/
│   ├── run_all.py                  # added 2026-07-30 — runs every validator + test file, one command
│   ├── unit/                       # fast, deterministic — no real model/compiler calls (Testing Strategy doc, Section 2)
│   │   ├── test_config_validation.py   # Phase 1
│   │   ├── test_state.py               # Phase 2
│   │   └── test_json_extraction.py     # Phase 3 — the Section 3.6 extractor vs the malformed battery
│   ├── integration/                # harness + fake model_client, config cross-validation
│   │   ├── test_harness_loop.py        # Phase 2 — call order, trace shape, edge cases
│   │   ├── test_persona_fallbacks.py   # Phase 2 — failure injection per persona (Section 5)
│   │   ├── test_real_persona_paths.py  # Phase 3 — real code paths via the fake client
│   │   ├── test_personas_live.py       # Phase 3 — needs a live model; skipped unless `run_all.py --live`
│   │   └── test_golden_trace.py        # structural regression guard (Testing Strategy doc, Section 2.3)
│   ├── fixtures/                   # golden trace logs, canned model responses
│   │   ├── fake_model_client.py        # Testing Strategy doc, Section 2.1
│   │   ├── golden_trace_mocked.jsonl   # known-good mocked run; regenerate deliberately
│   │   └── sample_trace_real.jsonl     # a REPRESENTATIVE real run (not a passing one) — evidence for Section 7.1f
│   └── reliability/                # scripts/run_reliability_report.py output lands here (gitignored, not committed)
├── logs/
│   └── (trace_<timestamp>.jsonl written here at runtime)
└── demo/
    ├── WRITEUP.md                  # Phase 7 — plain-English writeup, one-paragraph summary first
    ├── RECORDING_SHOTLIST.md       # Phase 7 — shot-list, if a narrated screen capture is wanted
    ├── trace_canonical.jsonl       # Phase 7 — the portfolio trace artifact (one clean real run)
    ├── logo.png                    # 2026-08-11 — square mark, drawn by scripts/render_logo.py
    ├── banner.png                  #   and the README header, using the renderer's own sprites
    ├── pixel_swarm_demo_full.gif   # Phase 7 — the 75s demo, all five beats
    └── pixel_swarm_demo.gif        # Phase 7 — 12s loop of the same run, for embedding
```
Added 2026-07-29 per `06_TESTING_STRATEGY.md` — see that doc for what belongs in each `tests/` subfolder and why it's organized this way.

**`pixel_world/` — added 2026-08-01, and the reason matters.** Every doc referred to "your existing Pixel-World Compiler". **It does not exist**: it was never built, and nothing by that name is downloadable — the name originates in these planning docs, not in any product. Phase 5 was therefore written against a false premise, which is why Interface Contract Section 3.2's four `[TODO]`s could never be answered. They were questions about a system that was not there.

Rather than abandon PRD success criterion 4 ("the compiler renders the resulting animation"), a minimal renderer stands in for it. It is deliberately a **separate top-level package, not part of `compiler_adapter/`**, so the two-system separation the Interface Contract is built around stays real rather than nominal:

- `pixel_world/` imports **nothing** from `swarm/` or `compiler_adapter/`. It accepts named states (`"alert"`, `"duck"`) and knows nothing about personas, ticks, or Directives.
- `compiler_adapter/adapter.py` is the only module that knows both sides, exactly as Interface Contract Section 3.3 specifies.

**The "no changes to compiler core" acceptance criterion is now weaker, and that should be stated plainly rather than glossed:** you cannot prove you did not modify code you wrote yourself. What remains verifiable is the criterion's *intent* — the renderer has no knowledge of the swarm, so the swarm cannot have reached into it — and that is enforced by an import-graph test rather than asserted in prose.

## 9. Tech stack (state explicitly so no session re-decides this)
- **Language:** Python (matches your existing Pixel-World Compiler stack).
- **Local model runtime:** whichever you already use for local inference (e.g. Ollama or llama.cpp) — pick one and put its name directly in `config/personas.json`, don't abstract over multiple runtimes for v1.
- **No new external dependencies** beyond what's needed for: the model runtime's Python client, JSON handling (stdlib), and your existing Pillow/NumPy/FFmpeg compiler stack. Resist adding a web framework, database, or queue system — v1 is a single-process script.
- **Config format:** plain JSON files (Section 4, `config/personas.json`) — no YAML/env-var layering needed at this scale.
