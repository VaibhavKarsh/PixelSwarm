"""Generate the project logo and README banner.

The logo is drawn with the project's OWN renderer - the same sprite sheet, the
same mood palette, the same nearest-neighbour upscale that produces every frame
of the demo. That is the point: a stock logo would say nothing, while this one
is literally the thing the repo makes. Regenerating it after a palette change
keeps the branding honest for free.

The wordmark uses a hand-built 5x7 pixel font rather than a system typeface,
because a smoothly antialiased font next to hard-edged sprite pixels looks like
a mistake. Only the ten letters of "PIXEL SWARM" are defined; add glyphs below
if the name ever changes.

Outputs (committed, because the README embeds them and a fresh clone should not
need Pillow to display correctly on GitHub):
    demo/logo.png      512x512  square mark - GitHub social preview, avatars
    demo/banner.png   1280x320  wide header for the top of the README

Usage:
    python scripts/render_logo.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit(
        "FAIL: this script needs Pillow (pip install -r requirements.txt).\n"
        "      The committed logo/banner mean nothing else in the repo depends on it."
    ) from None

from pixel_world.renderer import MOOD_SHIRT, Renderer  # noqa: E402

# --- 5x7 pixel font, uppercase, only the glyphs the wordmark needs ------------

FONT = {
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    " ": ["00000"] * 7,
}

# The four personas, in the order they run each tick. Their colours are the mood
# shirts, so the logo's palette is the animation's palette - not a second one
# invented for branding that would drift the first time a mood is retuned.
PERSONA_COLORS = [
    MOOD_SHIRT["idle"][1],
    MOOD_SHIRT["happy"][1],
    MOOD_SHIRT["excited"][1],
    MOOD_SHIRT["alert"][1],
]

INK = (28, 24, 40)
INK_SOFT = (58, 52, 78)


def text_width(text, spacing=1):
    return sum(len(FONT[c][0]) + spacing for c in text) - spacing


def draw_text(draw, text, x, y, color, spacing=1):
    """Blit the pixel font one logical pixel at a time."""
    cx = x
    for char in text:
        glyph = FONT[char]
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    draw.point((cx + col, y + row), fill=color)
        cx += len(glyph[0]) + spacing
    return cx


def vertical_gradient(draw, w, h, top, bottom, y0=0, y1=None):
    y1 = h if y1 is None else y1
    span = max(1, y1 - y0 - 1)
    for y in range(y0, y1):
        t = (y - y0) / span
        draw.line(
            [(0, y), (w, y)],
            fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )


def swarm_nodes(draw, cx, row_y, junction_y, head_y, spread, node_r=2):
    """Four persona nodes on a row above the character, converging on one point.

    The first attempt put them on a tight arc around the head and they read as
    antlers - the lines touched the sprite, so the eye merged them into it. Four
    nodes on a level row, funnelling into a single junction that then drops to
    the character, reads as what the system actually is: four separate deciders
    producing one committed pose. Keep a clear gap between `junction_y` and
    `head_y` or the antler effect returns.
    """
    step = spread // 3
    xs = [cx - spread // 2 + i * step for i in range(4)]

    for x in xs:                                   # feeder lines into the junction
        draw.line([(x, row_y), (cx, junction_y)], fill=INK_SOFT)
    draw.line([(cx, junction_y), (cx, head_y)], fill=INK_SOFT)   # the commit

    for x, color in zip(xs, PERSONA_COLORS):
        draw.ellipse([x - node_r, row_y - node_r, x + node_r, row_y + node_r],
                     fill=color, outline=INK)
    draw.ellipse([cx - 1, junction_y - 1, cx + 1, junction_y + 1],
                 fill=(255, 255, 255), outline=INK)
    return xs


def character_rgba(mood="happy", action="wave"):
    renderer = Renderer(hud=False)
    return renderer.draw_character(mood, action, frame=0)


def build_square(size=512):
    """The square mark: character, swarm nodes, no text."""
    W = H = 64  # logical pixels; 64 * 8 = 512
    img = Image.new("RGB", (W, H), MOOD_SHIRT["idle"][0])
    draw = ImageDraw.Draw(img)
    vertical_gradient(draw, W, H, (150, 206, 240), (206, 234, 248))

    # Ground sits low: an earlier version put it at 46 and left a third of the
    # mark as empty grass, which wastes the area an avatar has least of.
    ground_y = 53
    draw.rectangle([0, ground_y, W, H], fill=(126, 182, 122))
    draw.line([(0, ground_y), (W, ground_y)], fill=(96, 150, 96))

    sprite = character_rgba()
    sx = (W - sprite.width) // 2
    sy = ground_y - sprite.height + 1
    swarm_nodes(draw, W // 2, row_y=6, junction_y=19, head_y=sy - 1, spread=34)
    img.paste(sprite, (sx, sy), sprite)

    # a 1px frame keeps the mark from bleeding into light page backgrounds
    draw.rectangle([0, 0, W - 1, H - 1], outline=INK)
    return img.resize((size, size), Image.NEAREST)


def build_banner(width=1280, height=320):
    """The wide README header: mark on the left, wordmark on the right."""
    # scale 6 rather than 8: the mark needs vertical room above the head for the
    # node row, and a 320px-tall banner at scale 8 leaves only 40 logical pixels.
    scale = 6
    W, H = width // scale, height // scale  # 213 x 53 logical
    img = Image.new("RGB", (W, H), (150, 206, 240))
    draw = ImageDraw.Draw(img)
    vertical_gradient(draw, W, H, (146, 202, 238), (212, 236, 250))

    ground_y = H - 7
    draw.rectangle([0, ground_y, W, H], fill=(126, 182, 122))
    draw.line([(0, ground_y), (W, ground_y)], fill=(96, 150, 96))

    sprite = character_rgba()
    word = "PIXEL SWARM"
    gap = 16
    lockup_w = sprite.width + gap + text_width(word)
    sx = (W - lockup_w) // 2                       # centre the whole lockup
    sy = ground_y - sprite.height + 1
    swarm_nodes(draw, sx + sprite.width // 2, row_y=5, junction_y=15,
                head_y=sy - 1, spread=30)
    img.paste(sprite, (sx, sy), sprite)

    wx = sx + sprite.width + gap
    wy = sy + 6
    # drop shadow first, so the wordmark reads on the light sky
    draw_text(draw, word, wx + 1, wy + 1, (255, 255, 255))
    draw_text(draw, word, wx, wy, INK)

    # underline the wordmark in the four persona colours, in tick order
    ul_y = wy + 9
    ul_w = text_width(word)
    seg = ul_w // 4
    for i, color in enumerate(PERSONA_COLORS):
        x0 = wx + i * seg
        x1 = wx + ul_w if i == 3 else x0 + seg - 1
        draw.line([(x0, ul_y), (x1, ul_y)], fill=color)

    return img.resize((width, height), Image.NEAREST)


def main() -> int:
    out_dir = REPO_ROOT / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)

    square = build_square()
    square_path = out_dir / "logo.png"
    square.save(square_path, optimize=True)
    print(f"  {square_path.relative_to(REPO_ROOT)}  {square.size[0]}x{square.size[1]}  "
          f"{square_path.stat().st_size / 1024:.0f} KB")

    banner = build_banner()
    banner_path = out_dir / "banner.png"
    banner.save(banner_path, optimize=True)
    print(f"  {banner_path.relative_to(REPO_ROOT)}  {banner.size[0]}x{banner.size[1]}  "
          f"{banner_path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
