# Pixel Swarm — Product Requirements Document (v1)

## 1. One-line pitch
A swarm of small local language models acts as the "animation direction team" for a procedural pixel-art character, deciding live how the character should look, move, and react to incoming events — replacing hand-scripted state machines with a negotiated, model-driven decision loop.

## 2. Problem
Procedural pixel-art characters (in games, stream overlays, interactive demos) are usually driven by hand-coded state machines: `if chat_hype > threshold: play(celebrate)`. This is brittle, doesn't generalize, and doesn't scale past a handful of hardcoded rules. Pixel Swarm replaces that logic with a small team of local models, each responsible for one narrow decision, that negotiate a final animation directive every "tick."

## 3. Target user (for this project's purpose)
This is a portfolio/product project, not a client engagement. The "user" for v1 is:
- Primary: you, proving the architecture works end-to-end on a scripted demo.
- Secondary (framing for resume/interviews): indie game developers or streamers who want reactive pixel characters without building a full animation-logic team.

## 4. Goals for v1
- A pixel-art character that visibly changes mood, action, and dialogue in response to a scripted stream of external events, driven entirely by a swarm of local SLMs.
- A working negotiation harness that resolves disagreement between personas deterministically and explainably (you can always answer "why did it do that?").
- A recorded demo (video or live run) showing at least one clear conflict-and-resolution moment (e.g., mood says "sad," action-picker had already queued "jump" — show how the harness resolves it).
- Clean integration with the existing Pixel-World Compiler's rendering pipeline — no changes to the compiler's core rendering, only a new "directive input" path.

## 5. Non-goals for v1 (explicitly out of scope)
- No training or fine-tuning of models — pure prompting/inference over existing small local models.
- No live game or live stream integration — event input is a scripted/simulated event stream for v1.
- No multi-character scenes — one character only.
- No real-time performance guarantees (e.g. sub-100ms) — correctness and legibility of decisions matter more than latency for v1.
- No persistent memory/personality learning across sessions — each run starts fresh.
- No UI/dashboard beyond what's needed to observe and debug the swarm's decisions (a simple log/terminal view is enough).

## 6. Core user story (the demo scenario)
> A pixel-art character is idling. A scripted event stream simulates a stream chat and game state over ~2–3 minutes. As events arrive (chat goes quiet → chat spikes with hype → a "danger" game event fires → chat calms down), the character's mood, chosen action/animation, and an optional short dialogue line all update live, driven by the swarm's negotiated decisions — not by hardcoded rules.

## 7. Personas (the swarm, v1 scope)
1. **Mood-picker** — decides the character's current emotional state (e.g. idle, happy, alert, sad, excited) based on recent events.
2. **Action-picker** — decides what the character is *doing* right now (e.g. idle-loop, wave, jump, duck, celebrate), constrained by what moods/actions are valid together.
3. **Dialogue-line** — optionally proposes a short text/speech-bubble line matching the current mood+action (can be "no line this tick").
4. **Transition-checker** — the arbiter: checks whether the proposed mood/action/dialogue combo is a *valid and smooth* transition from the current state, and either approves it or forces a fallback (e.g. don't allow "sad" → "celebrate" in one tick without an intermediate step).

(Exact prompt design and model choice belongs in the Architecture doc, not here.)

## 8. Success criteria for v1

*Status 2026-08-09. Rates are over 20 real runs, scored by `scripts/run_reliability_report.py`; artifacts in `tests/reliability/`.*

- [x] End-to-end run completes on the full scripted event stream without crashing or freezing. — **20/20**
- [x] At least 3 distinct mood states and 3 distinct actions are observably triggered during one full demo run. — **18/20**
- [x] At least one genuine conflict is resolved by the transition-checker and is visible/loggable (not silently dropped). — **19/20**, scored strictly: a harness-level fallback does not count, since that fires when the checker *failed*
- [~] The compiler renders the resulting animation without modification to its core rendering logic — only a new directive-input adapter was added. — **Renders: yes.** The second half cannot be honestly claimed: *the Pixel-World Compiler this refers to never existed* (Interface Contract Section 3.2), so the renderer had to be written here, and you cannot prove you did not modify code you wrote yourself. What is provable, and is enforced by an import-graph test, is the criterion's intent: `pixel_world/` imports nothing from `swarm/`, so the swarm cannot have reached into it.
- [x] You can explain, for any tick in the log, why the swarm made the decision it made (traceable, not a black box). — every tick records all three proposals with their `reason` fields, the verdict and its reason, per-persona timings, and a per-persona `errors` entry. `demo/pixel_swarm_explained.mp4` is that claim animated, quoting the trace verbatim.

## 9. Risks / open questions to resolve before/during build
- Which local models are fast/small enough to run 4 personas per tick without unacceptable lag for a demo? (needs a quick benchmark pass early)
- How much shared context does each persona need vs. how much should stay isolated (to keep them fast and avoid one persona's reasoning leaking into another's)?
- What happens if two personas time out or return malformed output — what's the fallback default state?

## 10. Tech stack (summary — full detail in Architecture doc, Section 9)
Python, your existing local model runtime (Ollama/llama.cpp or equivalent), your existing Pixel-World Compiler's Pillow/NumPy/FFmpeg pipeline. No new frameworks, databases, or services added for v1 — see Architecture doc for the locked repo structure.

## 11. Relationship to future projects
Pixel Swarm establishes the "swarm-as-director" pattern. If this ships successfully, the same pattern is intended to generalize to **Sound Swarm** (an audio equivalent, later project) — but that is explicitly out of scope for this document and this build.
