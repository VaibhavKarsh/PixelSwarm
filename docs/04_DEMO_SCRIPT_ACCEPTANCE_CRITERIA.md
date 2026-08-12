# Pixel Swarm — Demo Script & Acceptance Criteria (v1)

This is the concrete target every vibe-coding session should be building toward. If you're ever unsure whether something is "done," check it against this document, not a general feeling.

## 0. File locations (per repo structure in Architecture doc, Section 8)
- The scripted event sequence itself is saved as `events/demo_sequence.json`, using the Event schema from the Interface Contract doc (Section 2.1) — the table below is the human-readable version of that file, not a separate format.
- Each demo run writes its trace log to `logs/trace_<timestamp>.jsonl` (format per Architecture doc, Section 6).
- The recorded video and writeup (Section 3 below) are saved under `demo/`.

## 1. The scripted event sequence (v1 demo)

A single ordered list of events, replayed on a timer (not real-time input), roughly 2–3 minutes long. Suggested sequence — adjust freely, but keep the shape (calm → building → conflict/peak → resolution → calm):

| Time (s) | Event | Intended effect to observe |
|---|---|---|
| 0 | (none — idle start) | Character in idle_loop, mood idle |
| 15 | `chat_calm` | No visible change (already idle) — proves the system doesn't over-react to redundant signals |
| 30 | `chat_hype_spike` (intensity 0.6) | Mood shifts toward happy/excited; action may shift to wave |
| 45 | `chat_hype_spike` (intensity 0.9) | Mood escalates to excited; action shifts to celebrate |
| 60 | `game_danger` (intensity 0.7) | **Conflict moment**: mood pulled toward alert/sad while action-picker may still be biased toward celebrate from momentum — this is where the transition-checker should visibly intervene |
| 75 | `game_danger` (intensity 0.9) | Mood settles to alert; action shifts to duck or look_around |
| 95 | `game_safe` | Mood begins recovering toward idle/happy |
| 115 | `chat_calm` | Mood and action settle back to idle/idle_loop |
| 130 | (idle_timeout) | Confirms system returns to and holds a stable idle state |

## 2. Acceptance criteria (must all pass to call v1 "done")

**Status as of 2026-08-11.** Scored automatically by `scripts/run_reliability_report.py --runs 20 --real all`; artifacts in `tests/reliability/`. Rates below are **20 full runs** on the shipped configuration `d3f620203e0a`.

**How much these numbers can be trusted, stated first.** Five n=10 samples were taken during one afternoon across three configurations:

| | C1 | C2 | C3 | C4 | C5 | all five |
|---|---|---|---|---|---|---|
| mood clause only, sample A | 10 | 9 | 10 | 9 | 9 | **7** |
| mood clause only, sample B | 10 | 7 | 10 | 10 | 10 | **7** |
| + checker clause, sample C | 10 | 9 | 9 | 10 | 8 | **6** |
| + checker clause, sample D | 10 | 10 | 9 | 10 | 10 | **9** |
| + checker clause, sample E | 10 | 9 | 10 | 10 | 9 | **8** |

Samples C, D and E are the **same configuration** and scored 6, 9 and 8. Nothing changed between them. Any conclusion drawn from a single n=10 in this project — including several drawn in earlier drafts of this document — is reading noise. Figures are therefore pooled, and where a claim matters it is stated as a mechanism that can be checked rather than a rate that cannot.

### Functional
- [x] Full sequence runs start to finish without crash, hang, or unhandled exception. — **20/20 (100%)**
- [x] At least 3 distinct moods and 3 distinct actions are observed across the run. — **20/20 (100%)**
- [x] The tick at t=60 (game_danger arriving during celebrate) produces a **visible transition-checker intervention** in the trace log — i.e., you can point to one specific line and say "here the harness overrode a proposal." — **20/20 (100%)**, scored strictly: a harness-level fallback does *not* count, because it fires when the checker failed, which is the opposite of the swarm resolving a conflict.
- [x] At least one event (`chat_calm` at t=15) produces **no change**, demonstrating the system isn't just reacting to every input blindly. — **20/20 (100%)**
- [x] Final state at the end of the run returns to an idle mood/action, showing the loop is stable, not just reactive. — **19/20 (95%)**

> **Runs passing all five simultaneously: 19/20 (95%), 95% CI [76%, 99%]** — measured 2026-08-11 on config `d3f620203e0a`, with 0.00 persona failures per run. **Phase 6's 80% bar is met.**
>
> **But not by as much as it looks.** Against the same criteria without the grounded-rejection guard (23/30, 77%), a Fisher exact test gives **p = 0.12** — 20 runs cannot establish a difference of that size, and this document has already been burned once by treating a point estimate as precise. It previously reported **17/20 (85%)** as clearing the bar; that interval was [64%, 95%], and samples of the same config later returned 6, 9 and 8 out of 10. Separating 85% from 75% at 80% power needs ~250 runs per arm, about 19 hours of inference.
>
> **What is established, because it is deterministic rather than statistical:** across those 20 runs, **24 of 24 rejections that cited no rule in the table were overruled, and 0 were honoured**; the guard fired **zero times at the t=60 conflict**, because that rejection is genuine. The mechanism is verifiable tick by tick in the trace. The pass-rate improvement is a consequence whose size is not. See Architecture doc 7.1q.

### Explainability
- [x] For any tick you pick at random from the trace log, you can answer "why did it decide this?" using only the logged reasons — no guessing, no re-running with print statements. — every tick records all three proposals with their `reason` fields, the verdict and its reason, per-persona timings, and an `errors` entry per persona.
- [x] The full trace log for one demo run is saved as an artifact (JSONL file) you can attach to a portfolio writeup. — `demo/trace_canonical.jsonl`, a run that passes all five criteria with zero persona failures.

### Integration
- [x] Compiler renders the resulting animation for the full run using only the Directive interface (Doc 3) — no manual/hardcoded overrides during the demo. — `scripts/render_trace.py`; `RenderAdapter.send` is the identical call the harness makes live, and a test drives it both ways.
- [~] No changes were made to the compiler's core rendering logic — only the new adapter layer. — **This criterion cannot be met as written, and saying so is more useful than ticking it.** The Pixel-World Compiler never existed (Interface Contract Section 3.2), so the renderer had to be written here; you cannot prove you did not modify code you wrote yourself. What replaces it is the criterion's *intent*, enforced mechanically: `pixel_world/` imports nothing from `swarm/`, checked by an import-graph test.

### Presentation-readiness (for resume/portfolio use)
- [x] A short screen recording (60–90 sec) exists showing: idle → hype building → the danger conflict moment → recovery → idle. — `demo/pixel_swarm_demo_full.gif`, **69 seconds** (344 frames at 200 ms), all five beats in order. **It is a rendered animation, not a screen capture** — generated deterministically from `demo/trace_canonical.jsonl` through the same Directive path the harness uses live. Reproducible, no editing, no pacing problem; but not literally a capture, which is worth saying rather than glossing.
- [x] **Beyond this line's ask:** `demo/pixel_swarm_explained.mp4`, a **4 min 16 s walkthrough** that puts the swarm's reasoning on screen next to the animation — the event, each persona's proposal with its stated reason, the arbiter's verdict, and the committed state, revealed in sequence. It exists because narration was not available, and it turns out to beat a voiceover on one axis: **every word is quoted from the trace log**, so it cannot drift from what the system did. The character holds its previous pose while the personas deliberate and only changes when the verdict lands, which is what makes the t=60 override legible on screen. This is also the practical answer to the "explainability" criteria above — it is those claims, animated.
- [x] A one-paragraph plain-English writeup exists describing the architecture, referencing the conflict-resolution moment specifically. — `demo/WRITEUP.md` opens with a single-paragraph summary leading on the t=60 tick, followed by the longer account.
- [x] The trace log JSONL and a short README are in the repo so a reviewer could, in principle, verify your explainability claims themselves. — `demo/trace_canonical.jsonl` plus `README.md`.

> **On the canonical trace.** Four fresh candidate runs were produced for Phase 7 task 1 and compared against the existing artifact. The existing one was kept, and the reasoning is stated rather than hidden: the best new candidate reached 5 distinct moods but held `excited` through the danger event before jumping to `angry`, while the kept trace turns `alert` at t=60 as this document describes and reaches 4 distinct actions. **The artifact is illustrative; the statistics above are the aggregate over 20 runs.** Both are published, so a reviewer can check the claim against the distribution rather than the showcase.

## 3. What "not done yet" looks like (do not ship past this without going back to the PRD)
- Adding more moods/actions "because it'd be cool" before the above criteria pass — resist scope creep until v1 is demoable.
- Wiring in a real live chat/game connection — explicitly deferred (see PRD non-goals).
- Trying to make ticks real-time/low-latency before correctness is proven — sequence-correctness first, speed later.
