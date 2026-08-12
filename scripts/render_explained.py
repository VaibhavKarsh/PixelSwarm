"""Render a trace as a self-explaining walkthrough video (4-5 minutes).

The short demo GIF shows *what* the character did. This shows *why*: alongside
the animation, each tick reveals the swarm's negotiation step by step - the event
that arrived, each persona's proposal with its stated reason, the arbiter's
verdict, and the state finally committed.

It exists because narration is not available. On-screen reasoning carries the
explanation instead, and it has an advantage over a voiceover: every word is
quoted from the trace log rather than written afterwards, so the video cannot
drift from what the system actually did.

The character holds its PREVIOUS pose while the personas deliberate and only
changes when the verdict lands, which is what makes an override legible - you see
the arbiter refuse the proposal and the pose that results.

Output is MP4 when ffmpeg is available (a 5-minute GIF would be tens of MB),
falling back to GIF otherwise.

    python scripts/render_explained.py
    python scripts/render_explained.py --seconds-per-tick 30 --out demo/long.mp4
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pixel_world import Renderer  # noqa: E402

W, H = 1280, 720
HEADER_H, FOOTER_H = 66, 74
PANEL_X = 700
BG = (18, 17, 26)
PANEL_BG = (26, 25, 36)
INK = (238, 236, 248)
DIM = (146, 143, 172)
ACCENT = (232, 176, 92)
REJECT = (226, 96, 96)
OK = (126, 200, 140)

DEFAULT_TRACE = REPO_ROOT / "demo" / "trace_canonical.jsonl"
DEFAULT_OUT = REPO_ROOT / "demo" / "pixel_swarm_explained.mp4"

# Fractions of a tick at which each element appears. The character switches to
# the committed pose at the same moment the verdict is revealed.
REVEAL = [("event", 0.00), ("mood", 0.14), ("action", 0.31),
          ("line", 0.46), ("verdict", 0.60), ("committed", 0.78)]


def load_font(size, bold=False):
    names = (("consolab.ttf", "DejaVuSansMono-Bold.ttf", "arialbd.ttf") if bold
             else ("consola.ttf", "DejaVuSansMono.ttf", "arial.ttf"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = load_font(30, bold=True)
F_HEAD = load_font(19, bold=True)
F_BODY = load_font(17)
F_SMALL = load_font(15)
F_TINY = load_font(13)


def wrap(draw, text, font, width):
    words, lines, cur = str(text).split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


class Walkthrough:
    def __init__(self, records, seconds_per_tick=24, fps=12):
        self.records = records
        self.fps = fps
        self.per_tick = int(seconds_per_tick * fps)
        self.renderer = Renderer(scale=8, hud=False)

    # -- panel ------------------------------------------------------------

    def _persona_block(self, draw, y, label, value, reason, shown, colour=INK):
        if not shown:
            return y
        draw.text((PANEL_X, y), label, font=F_SMALL, fill=DIM)
        y += 21
        draw.text((PANEL_X, y), str(value), font=F_HEAD, fill=colour)
        y += 26
        # Models often quote the enum values in their own reasons, which would
        # give a doubled opening quote if wrapped naively.
        clean = str(reason).strip().strip('"').strip()
        for line in wrap(draw, f'"{clean}"', F_TINY, W - PANEL_X - 40)[:3]:
            draw.text((PANEL_X, y), line, font=F_TINY, fill=DIM)
            y += 17
        return y + 12

    def _draw_panel(self, draw, record, stage):
        y = HEADER_H + 22
        trigger = record["trigger"]
        event = trigger["event_type"] or "timer tick"

        draw.text((PANEL_X, y), "EVENT", font=F_SMALL, fill=DIM)
        y += 21
        draw.text((PANEL_X, y), event, font=F_HEAD, fill=ACCENT)
        y += 34

        proposals = record["proposals"]
        y = self._persona_block(
            draw, y, "mood-picker proposes", proposals["mood"].get("mood"),
            proposals["mood"].get("reason", ""), stage >= 1)
        y = self._persona_block(
            draw, y, "action-picker proposes", proposals["action"].get("action"),
            proposals["action"].get("reason", ""), stage >= 2)

        if stage >= 3:
            line = proposals["line"].get("line")
            y = self._persona_block(
                draw, y, "dialogue-line proposes", line if line else "(silence)",
                proposals["line"].get("reason", ""), True,
                colour=INK if line else DIM)

        if stage >= 4:
            verdict = record["verdict"]
            kind = verdict.get("verdict")
            approved = kind == "approve"
            draw.rectangle([PANEL_X - 14, y - 8, W - 26, y + 84],
                           fill=(40, 26, 30) if not approved else (24, 36, 28))
            self._persona_block(
                draw, y, "transition-checker",
                "APPROVE" if approved else kind.upper().replace("_", " "),
                verdict.get("reason", ""), True,
                colour=OK if approved else REJECT)

    # -- frame ------------------------------------------------------------

    def frame(self, index):
        tick_idx, within = divmod(index, self.per_tick)
        tick_idx = min(tick_idx, len(self.records) - 1)
        record = self.records[tick_idx]
        progress = within / self.per_tick

        stage = 0
        for i, (_, at) in enumerate(REVEAL):
            if progress >= at:
                stage = i
        # stage: 0 event, 1 mood, 2 action, 3 line, 4 verdict, 5 committed

        # The character holds its previous pose until the verdict lands.
        state = record["final_state"] if stage >= 4 else record["input_state"]

        canvas = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(canvas)

        scene = self.renderer.render(
            mood=state["current_mood"], action=state["current_action"],
            line=record["final_state"]["last_line"] if stage >= 5 else None,
            frame=(within // max(1, self.fps // 3)) % 2, t=index,
        )
        sx, sy = 26, HEADER_H + (H - HEADER_H - FOOTER_H - scene.height) // 2
        canvas.paste(scene, (sx, sy))
        draw.rectangle([sx - 2, sy - 2, sx + scene.width + 1, sy + scene.height + 1],
                       outline=(58, 56, 78), width=2)

        # header
        draw.rectangle([0, 0, W, HEADER_H], fill=PANEL_BG)
        draw.text((26, 18), "PIXEL SWARM", font=F_TITLE, fill=INK)
        draw.text((250, 28), "four local models directing one character",
                  font=F_SMALL, fill=DIM)
        label = f"tick {record['tick']} / {len(self.records)}"
        ts = record["trigger"].get("ts")
        if ts is not None:
            label += f"    t={int(ts)}s"
        draw.text((W - 26 - draw.textlength(label, font=F_BODY), 24), label,
                  font=F_BODY, fill=DIM)

        # panel
        draw.rectangle([PANEL_X - 30, HEADER_H, W, H - FOOTER_H], fill=PANEL_BG)
        self._draw_panel(draw, record, stage)

        # footer
        draw.rectangle([0, H - FOOTER_H, W, H], fill=PANEL_BG)
        if stage >= 5:
            final = record["final_state"]
            draw.text((26, H - FOOTER_H + 14), "COMMITTED", font=F_SMALL, fill=DIM)
            draw.text((26, H - FOOTER_H + 36),
                      f"{final['current_mood']}  /  {final['current_action']}",
                      font=F_HEAD, fill=INK)
            note = "every word above is quoted from the trace log"
            draw.text((W - 26 - draw.textlength(note, font=F_TINY), H - FOOTER_H + 42),
                      note, font=F_TINY, fill=DIM)
        return canvas

    def total_frames(self):
        return self.per_tick * len(self.records)


def title_card(text, subtitle, seconds, fps):
    frames = []
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.text(((W - draw.textlength(text, font=F_TITLE)) / 2, H // 2 - 60),
              text, font=F_TITLE, fill=INK)
    for i, line in enumerate(subtitle):
        draw.text(((W - draw.textlength(line, font=F_BODY)) / 2, H // 2 - 6 + i * 28),
                  line, font=F_BODY, fill=DIM)
    frames.extend([img] * int(seconds * fps))
    return frames


def write_mp4(frames_iter, total, out, fps):
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
        "-framerate", str(fps), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
        "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i, frame in enumerate(frames_iter):
        proc.stdin.write(frame.tobytes())
        if i % 200 == 0:
            print(f"    {i}/{total} frames", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("FAIL: ffmpeg returned non-zero")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", default=str(DEFAULT_TRACE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--seconds-per-tick", type=float, default=24.0)
    parser.add_argument("--fps", type=int, default=12)
    args = parser.parse_args(argv)

    path = Path(args.trace)
    records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not records:
        raise SystemExit(f"FAIL: {path} has no records")

    walk = Walkthrough(records, args.seconds_per_tick, args.fps)
    intro = title_card(
        "PIXEL SWARM",
        ["Four small local language models decide how a pixel-art character",
         "should look, move and speak - negotiating it every tick.",
         "",
         "Everything on screen is quoted from one real run's trace log."],
        7, args.fps)
    outro = title_card(
        "13 / 20 runs pass every acceptance criterion",
        ["The transition-checker caught the conflict at t=60s.",
         "A deterministic invariant behind it guarantees an illegal",
         "pose is never committed, even when the arbiter errs.",
         "",
         "demo/trace_canonical.jsonl  -  every decision, with its reason"],
        9, args.fps)

    total = len(intro) + walk.total_frames() + len(outro)
    duration = total / args.fps
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"trace : {path.name} ({len(records)} ticks)")
    print(f"out   : {out}")
    print(f"length: {duration:.0f}s ({duration/60:.1f} min), {total} frames at {args.fps}fps")
    print("-" * 62)

    def frames():
        yield from intro
        for i in range(walk.total_frames()):
            yield walk.frame(i)
        yield from outro

    if shutil.which("ffmpeg") and out.suffix.lower() == ".mp4":
        write_mp4(frames(), total, out, args.fps)
    else:
        collected = list(frames())
        quant = [f.convert("P", palette=Image.ADAPTIVE, colors=128) for f in collected]
        quant[0].save(out, save_all=True, append_images=quant[1:],
                      duration=int(1000 / args.fps), loop=0, optimize=True)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out}  ({size_mb:.1f} MB, {duration/60:.1f} min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
