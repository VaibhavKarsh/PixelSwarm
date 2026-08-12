# Recording shot-list (Phase 7)

Prepared so the recording can be made in one sitting once there is something to
render. **This step is blocked on Phase 5** — there is no Pixel-World Compiler in
this repo or on this machine, so nothing draws a character yet. Everything below
assumes that exists.

Demo Script Section 3 asks for 60–90 seconds showing: idle → hype building → the
danger conflict → recovery → idle.

## Before recording

```bash
python tests/run_all.py            # must be all green
python scripts/validate_configs.py # config matches the docs
```

Have `logs/` empty or note the timestamp, so the trace you record is the trace you keep.

## The pacing problem, and the options

A real run takes **~5.5 minutes** for a 130-second event sequence: each tick makes
four model calls at ~10s each. Recording it at native speed gives a 5-minute video
of a mostly-still character. Three ways to handle it, in the order I'd try them:

1. **Record natively and speed up in post (recommended).** Roughly 4× gets 5.5 min
   into ~80 seconds. PRD Section 5 makes latency an explicit non-goal, so this
   changes nothing about correctness, and the trace log stays honest.
2. **Widen the event spacing** in `events/demo_sequence.json` so ticks finish
   comfortably between events. Costs a longer demo and changes doc 04's table,
   which `scripts/validate_configs.py` cross-checks — update both together.
3. **Use a faster model.** Not recommended: `gemma4:e2b` runs a tick in ~6s but
   rejects every proposal indiscriminately (Section 7.1l), so the t=60 beat breaks.

## Shots

| # | Beat | What must be on screen | Roughly |
|---|---|---|---|
| 1 | Idle start | character in `idle_loop`, mood `idle` | 0–15s |
| 2 | Redundant signal ignored | `chat_calm` arrives at t=15 and **nothing changes** — this is the "not just reacting to every input" beat | 15–30s |
| 3 | Hype builds | mood → `happy` then `excited`; action → `wave` then `celebrate` | 30–60s |
| 4 | **The conflict** | danger fires mid-celebration; character drops to `idle_loop` rather than snapping to a scan. **Hold on this.** | 60–75s |
| 5 | Alert settles | `look_around` from the neutral pose | 75–95s |
| 6 | Recovery | `game_safe` → mood lifts, action returns toward `wave`/`idle_loop` | 95–115s |
| 7 | Stable idle | settles and holds `idle`/`idle_loop` through the trailing timer ticks | 115s–end |

## Shot 4 is the whole video

Everything else is context for it. If the run you record doesn't produce a genuine
checker override at t=60, **re-record** — it happens in about 9 runs out of 10, so
a second take is cheap. Verify before editing:

```bash
python -c "import json,glob; p=sorted(glob.glob('logs/trace_*.jsonl'))[-1]; r=[json.loads(l) for l in open(p,encoding='utf-8')]; t=[x for x in r if x['trigger']['ts']==60.0][0]; print(t['verdict'])"
```

You want `verdict: reject`, and `errors.check` to be `null` — if `errors.check` is
populated the harness invariant did the work rather than the swarm, which is a
weaker story and should not be narrated as the models resolving it.

## Suggested voiceover for shot 4

> "Chat has been hyping for thirty seconds and the character is mid-celebration.
> A danger event fires. The mood-picker turns alert. The action-picker proposes
> looking around — which is right. And the arbiter rejects it, because you can't
> cut from a celebration straight into a scan. It drops to a neutral pose for one
> tick first. Nothing in the code says that. Four models negotiated it, and the
> log says why."

## Also capture

A few seconds scrolling the trace log for the t=60 tick, showing the three
proposals with their `reason` fields next to the verdict. That is the
explainability claim made concrete, and it is more convincing than the animation.
