# Pixel Swarm — what it is, and the moment it's built around

## In one paragraph

Pixel Swarm replaces the hand-written state machine that would normally drive a pixel-art character with four small local language models that negotiate its direction every tick: a mood-picker reads the recent event stream, an action-picker chooses what the body is doing, a dialogue-line optionally speaks, and a transition-checker arbitrates between them against a small table of disallowed transitions. The moment that makes it worth building happens sixty seconds into the scripted run — the character is mid-celebration when a danger event fires, the mood turns `alert`, the action-picker correctly proposes scanning, and the checker **rejects it**, because you cannot cut from a celebration straight into a scan; it substitutes a neutral pose for one beat and the character picks up scanning from there. No line of code encodes that behaviour. Four models negotiated it, each recorded its stated reason in the trace log. Two deterministic invariants sit behind them: an illegal pose is never committed, and — because the checker turned out to reject legal actions on invented rules most of the time — a rejection that cites no rule in the table is never honoured.

*(The rest of this document expands on that, reports what works and what does not, and records the methodology mistakes that cost the most time — including publishing a number far more precise than the measurement behind it.)*

## The moment

Sixty seconds into a scripted event stream, the character is mid-celebration — chat has been hyping for half a minute — when a danger event fires.

The mood-picker turns `alert`. The action-picker, correctly, proposes `look_around`: that is what an alert character does about a threat. And the transition-checker **rejects it** — you cannot cut from a celebration straight into a scan — so it substitutes `idle_loop`. The character drops to a neutral pose, and picks up scanning a couple of ticks later.

Verbatim from [`trace_canonical.jsonl`](trace_canonical.jsonl), tick 4:

```
input state      excited / celebrate
proposed mood    alert        "Active danger signal overrides previous excitement."
proposed action  look_around  "Danger detected, character should look around to assess threat"
verdict          reject       "look_around is disallowed after celebrate"
committed        alert / idle_loop
errors           {mood: null, action: null, line: null, check: null}
```

That last line matters: no persona failed and the harness's own safety net never fired.
The models resolved it themselves.

No line of code says "when danger interrupts a celebration, insert a neutral beat." Four small local language models negotiated it, and the log records why each of them decided what it did.

## What it is

A swarm of local LLMs acting as the animation direction team for a pixel-art character, replacing the hand-written state machine that would normally drive it. Every tick, four personas run in sequence, each seeing the ones before it:

| Persona | Decides | Sees |
|---|---|---|
| **mood-picker** | the emotional state | recent events + committed state |
| **action-picker** | what the body is doing | the above, plus the proposed mood |
| **dialogue-line** | an optional spoken line (usually none) | the above, plus the proposed action |
| **transition-checker** | approve, or reject with a fallback | all three proposals + the rules table |

The transition-checker is the arbiter. It is given a small table of disallowed transitions and asked to judge the other three. Behind it sit two deterministic invariants in the harness, one in each direction: whatever any model returns, an action the table forbids is never **committed**, and a rejection the table does not support is never **honoured**.

That backstop has an honest history worth recording, because it changed. Under the original model and prompts it fired **2–3 times per 10 runs** — the checker diagnosed conflicts correctly but then named an illegal fallback, and the invariant was doing real work. After moving to `ornith:9b` and closing the prompt gaps, it has fired **zero times in 98 runs** across four configurations. It is now a dormant safety net rather than an active participant. Both facts matter: it earned its place, and it no longer needs to.

The *checker-level* fallback is a different story and did fire: one run in fifty saw the transition-checker itself time out, and the harness committed its Section 5 last resort. That path is not dead code — it is just rare.

Every tick writes one JSON line: inputs, all three proposals with their stated reasons, the verdict, the committed state, per-persona timings, and any failures. Pick any tick at random and you can answer "why did it do that" without re-running anything.

## How well it works

Measured over **20 full runs** of the shipped configuration (`d3f620203e0a`) with all four personas on a real local model (`ornith:9b` via Ollama), scored automatically by `scripts/run_reliability_report.py`:

| | Shipped (20 runs) | Without the grounding guard (30 runs) |
|---|---|---|
| Runs completing without crash | 20/20 | 30/30 |
| Redundant event at t=15 correctly ignored | 20/20 | 30/30 |
| Genuine checker override at the conflict tick | 20/20 | 28/30 |
| 3+ distinct moods and 3+ distinct actions | 20/20 | 28/30 |
| Run settles back to a stable idle state | 19/20 | 27/30 |
| Runs passing *every* criterion | **19/20 (95%)**, CI [76%, 99%] | 23/30 (77%), CI [59%, 88%] |
| Persona failures (malformed output, timeouts) | **0.00 per run** | 0.03 per run |
| Ungrounded rejections | 24/46 — **all 24 overruled** | 48/78 — all honoured |

**Read the 95% carefully.** Against the 77% column it looks decisive, and against a Fisher exact test it is not: **p = 0.12**. Twenty runs cannot establish a difference that size. What *is* established is the mechanism, because it is deterministic rather than statistical: **24 of 24 ungrounded rejections were overruled, and 0 slipped through**. The guard provably converts a rejection-with-no-rule into the action-picker's proposal. The pass-rate improvement is a consequence whose magnitude remains uncertain; the behaviour it corrects is not in doubt.

That distinction is the most useful methodological thing this project taught me, and it was learned the hard way. An earlier version of this document published **85%** and called the bar met. That was 17/20 — real, but with a 95% interval of [64%, 95%]. Three independent n=10 samples of the *identical* configuration then came back **6/10, 9/10, 8/10**: same code, same model, same machine, same afternoon. One distribution sampled three times, not three results.

Separating 85% from 75% at 80% power needs roughly **250 runs per arm**, about 19 hours of inference. Every prompt decision in this project was made on n=10 or n=20, so **none was validated to the precision its write-up implied** — including the ones I was most confident about. Prefer a mechanism you can verify (24/24) over a rate you cannot (p=0.12).

It sat at 65% for a long time, and what moved it is still worth recording — with the caveat above that the before/after numbers are n=20 point estimates with wide intervals, so the size of the improvement is far less certain than the diagnosis. A **second event scenario** — written only to check the swarm was not tuned to one script — got the character stuck: nine consecutive ticks in `alert`, through two separate all-clear events, never recovering. The model's own logged reason gave away why. At a tick whose most recent event was an all-clear, it wrote *"Recent danger event overrides earlier positive events"* — with the ordering backwards. The prompt said danger outranks hype but never said anything about ordering **between threat signals**, so any danger inside the 60-second memory window pinned the mood indefinitely. It was following the instruction correctly; the instruction was incomplete.

One sentence fixed it, and it lifted the canonical run too.

**Why that clause worked where three earlier ones failed** is the transferable part. Each earlier attempt adjusted a *preference* — how eagerly to read calm as happy, how strictly to reject a proposal — and every one traded a criterion for another, because moving a threshold moves everything that threshold touches. This one supplied a **missing rule** that nothing else depended on. Stating something a spec assumed is safe; shifting a threshold is a trade.

## The thing I nearly shipped without noticing

Every number above is a *pass rate*. None of them can answer the question the project is actually making a claim about: when the transition-checker rejects a proposal, **is the rejection backed by the rules table, or did the model make it up?**

So I measured it — re-deriving the legal set for every rejection in a 20-run baseline:

| | |
|---|---|
| Rejections of an action the table forbids | 20/71 |
| **Rejections of a perfectly legal action** | **51/71 (72%)** |
| Runs containing at least one | 19/20 |

The reasons are not misreadings, they are inventions:

> *"Wave is disallowed when mood is excited due to alert mood constraint"* — the mood was `excited`; it borrowed `alert`'s rule.
> *"Duck disallowed after idle_loop"* — no such rule exists; only `celebrate` and `jump` have entries.

**No acceptance criterion could ever have caught this**, and that is the interesting part. A wrongly-rejected *legal* action still commits `idle_loop`, which is legal — so the deterministic invariant stays silent and all five criteria still pass. The system was degrading safely and incorrectly at the same time, and the only reason I found it was deciding to check a claim nobody had written a test for.

The cause was the same shape as every other prompt bug here: the checker prompt explained how to reject but **never said the table was exhaustive**. Adding one sentence — a mood with no entry restricts nothing; never infer a restriction because it seems plausible — cut fabricated rejections by a quarter and doubled the approval rate for legal `alert` actions, from 19% to 38%.

That also **overturned a documented dead end.** This writeup used to say no installed model would ever approve a legal `alert` action — 0/8 for all seven models from 2B to 9B, probed in both directions. That probe was real, but it was measuring a prompt gap and calling it a capability limit. The canonical trace above now shows the character scanning (`alert` / `look_around`) at ticks 6 and 7, which the old configuration never once produced.

Prompting did not finish the job — the checker still invented rules on about half its rejections — so the second invariant was added deliberately: **a rejection whose action is legal is not honoured.**

That sounds like it betrays the premise, so here is the argument that it does not. Every rule in the table forbids an **action**; there is no rule kind that can forbid a mood or a line. So when the rejected action is legal, *no rule in the table could have justified rejecting it*. The harness is not substituting its taste for the model's — it is requiring the veto to cite the rules the model was handed, which is exactly the standard the first invariant already applied in the other direction. The scope is kept as narrow as that argument: the **mood is never overruled**, because the table cannot adjudicate moods and overriding one really would be the harness inventing policy.

It stands down where it should. Over 20 runs it fired 24 times and **not once at the t=60 conflict**, because that rejection is real. The demo's beat is produced by the swarm, not preserved by an exception.

What it does *not* fix is the checker's reasoning. Tick 6 above still contains a model that argues itself out of its own objection and rejects anyway; the guard corrects the outcome, not the thought. So the reliability report keeps printing the ungrounded-rejection rate as a measure of the **model**, separately from the count the guard overruled — because hiding it once the symptom is handled would discard the only signal that would say whether the underlying behaviour ever improves.

## What was hard, and what it taught

**The conflict didn't happen on its own.** The first design encoded only *pairing* rules — "while alert, don't celebrate." But the action-picker is handed the mood and a pairing guide, so it proposes a mood-appropriate action every time; a pairing rule can only fire when the action-picker misbehaves, which the real model doesn't. Four runs produced zero overrides. The fix was to add *smoothness* rules keyed on the action the character is currently performing, so the conflict arises from the personas behaving **well** rather than badly.

**A benchmark that measures parts can't predict the whole.** This bit three times. The first conflict probe fed the checker a conflict directly — proving it could *resolve* one, never that one would *occur*. A per-persona latency probe measured each model already loaded, missing that splitting personas across two 6GB models makes the runtime swap them twice per tick — a 3× slowdown. And a one-directional probe scored `gemma4:e2b` as a strong checker when it simply rejects everything.

**One bug lived between two documents.** The event schema gave "nothing is happening" signals an intensity of `1.0`, meaning "fully present." The mood prompt read intensity as *strength*: "0.8 or above is excited." Each document was reasonable alone; together they handed the model a maximum-strength signal for an absence of stimulus. Three prompt rewrites failed against it because they were arguing with a number in the payload. Changing it to `0.1` fixed two acceptance criteria at once.

## Running it

```bash
python tests/run_all.py                  # 15 checks, 260 tests, no model needed
python -m swarm.harness                  # full sequence, personas mocked, instant
python -m swarm.harness --real all       # against real models (~5.5 min)
python scripts/run_reliability_report.py --runs 10 --real all
```

Each run writes `logs/trace_<timestamp>.jsonl`.

## The artifact

[`demo/trace_canonical.jsonl`](trace_canonical.jsonl) is one complete real run, ten ticks,
one JSON object per line. It passes every acceptance criterion, reaches four distinct
moods and four distinct actions, and contains the t=60 override quoted above with no
persona failures anywhere in the run. Every record carries `config_version`
`d3f620203e0a`, and [`trace_canonical.meta.json`](trace_canonical.meta.json) says what
that expands to, so the run is tied to the exact configuration that produced it:

```
 1 chat_calm        idle     idle_loop
 2 chat_hype_spike  happy    wave         "Hehe! Hi!"
 3 chat_hype_spike  excited  celebrate    "Yay!"
 4 game_danger      alert    idle_loop     <-REJECT
 5 game_danger      alert    look_around   <-REJECT (OVERRULED)
 6 game_safe        alert    wave          <-REJECT (OVERRULED)
 7 chat_calm        happy    wave         "We're safe! Let's party!"
 8 idle_timeout     happy    wave
 9 timer            idle     idle_loop
10 timer            idle     idle_loop
```

Ticks 4, 5 and 6 are the whole architecture in three lines. **Tick 4 is a real rule** — you cannot cut from a celebration to a scan — so the rejection stands and the character drops to a neutral beat. **Ticks 5 and 6 are inventions**, and the harness overrules both: at tick 5 the checker claimed *"look_around disallowed after idle_loop in alert mood"*, which is not a rule that exists.

Tick 6 is the one worth reading twice. The checker's own logged reason is:

> *"Happy mood is allowed but wave is disallowed after look_around when alert; no rule violation found actually - happy and wave are both valid. Let me recheck."*

It talks itself out of its own objection, states plainly that both proposals are valid — **and returns `reject` anyway.** That is not a misreading of the rules. The verdict and the reasoning have come apart, and no amount of prompt work reliably prevents it. The harness commits `wave`, because the table forbids nothing here.

Read any line and the reasoning is there — that is the claim this project is really making,
and it is checkable without taking anyone's word for it.

## What is not here yet

The renderer. The Pixel-World Compiler this is designed to drive does not exist in this
repo, so `compiler_adapter/adapter.py` is a stub and no character is drawn. The Directive
interface it would consume is specified and exercised — every tick emits one — but nothing
consumes it yet, and the 60–90 second demo video is blocked behind that. The shot-list is
written and ready: [`RECORDING_SHOTLIST.md`](RECORDING_SHOTLIST.md).
