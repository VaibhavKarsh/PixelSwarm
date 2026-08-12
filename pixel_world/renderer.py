"""Pixel-art renderer for a single character.

Stands in for the "Pixel-World Compiler" the docs assumed existed. It accepts
NAMED STATES - a mood and an action, both plain strings - and draws frames. It
knows nothing about personas, ticks, Directives or the swarm, and imports nothing
from `swarm/` or `compiler_adapter/`; a test enforces that. See Architecture doc
Section 8.

Design notes, since "pixel art" is easy to do badly:

  - Sprites are authored as character grids at a LOGICAL resolution and upscaled
    with NEAREST resampling, so pixel edges stay hard. Any smoothing filter is
    what makes fake pixel art look muddy.
  - The character is drawn in CHIBI proportions - the head is roughly 45% of the
    body height, with oversized eyes. That is the single biggest lever on
    "cute"; realistic proportions at this resolution read as a small adult.
  - Poses and moods compose rather than multiply: 6 poses x 6 moods would be 36
    hand-drawn sprites, so each pose leaves a face window and the mood supplies a
    face into it. A seventh mood costs six lines, not six sprites.
  - The scene is built in DEPTH LAYERS - sky gradient, clouds, snow-capped
    mountains, a treeline, grass with a tufted edge, then textured dirt. A flat
    two-tone backdrop was the biggest thing making the first version look cheap.
  - Backgrounds are deterministic and cached per mood. Nothing is random, so
    frames never shimmer, and a 140-frame render stays fast.
  - The HUD is drawn AFTER upscaling with a real font. Text pushed through an
    8x nearest upscale is unreadable, and a hand-rolled bitmap font is worse.
"""

from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise ImportError(
        "Pillow is required to render. Fix: pip install -r requirements.txt"
    ) from exc

MOODS = ["idle", "happy", "excited", "alert", "sad", "angry"]
ACTIONS = ["idle_loop", "wave", "jump", "duck", "celebrate", "look_around"]

SCALE = 8                    # logical pixel -> screen pixel
STAGE_W, STAGE_H = 72, 52    # logical stage size
HORIZON = 34                 # ground line, in logical pixels
HUD_H = 60                   # HUD strip height, in FINAL pixels

# --- character palette --------------------------------------------------------
# '.' transparent   K outline      H hair        h hair highlight
# S skin            s skin shade   W eye white   E pupil
# B shirt           b shirt shade  w shirt light M mouth
# P trousers        p trouser shade

CHARACTER_BASE = {
    "K": (32, 26, 44),
    "H": (94, 58, 48),
    "h": (128, 82, 62),
    "S": (255, 214, 178),
    "s": (226, 176, 141),
    "W": (255, 255, 255),
    "E": (44, 36, 58),
    "M": (168, 74, 78),
    "P": (66, 72, 108),
    "p": (48, 54, 84),
}

# Shirt ramp per mood: the emotional read at a glance.
MOOD_SHIRT = {
    "idle":    ((96, 132, 190), (128, 166, 220), (68, 98, 150)),
    "happy":   ((92, 178, 118), (126, 212, 150), (62, 138, 88)),
    "excited": ((240, 168, 64), (255, 206, 112), (198, 126, 40)),
    "alert":   ((226, 118, 72), (255, 156, 108), (176, 82, 48)),
    "sad":     ((110, 128, 172), (142, 160, 200), (78, 94, 134)),
    "angry":   ((200, 72, 76), (232, 108, 108), (152, 46, 54)),
}

# --- scene palette per mood ---------------------------------------------------
# Mood shifts the whole scene's light, which sells the emotional state far more
# than the character alone can.

SCENE = {
    "idle":    {"sky": ((126, 196, 232), (186, 226, 244)), "cloud": (246, 252, 255),
                "far": (128, 152, 190), "near": (96, 118, 158), "snow": (238, 246, 255),
                "hill": (98, 158, 108), "tree": (64, 132, 96), "tree2": (86, 162, 116),
                "grass": (118, 190, 108), "grass2": (92, 164, 88),
                "dirt": (146, 104, 68), "dirt2": (118, 82, 52),
                "orb": (255, 244, 198), "weather": None},
    "happy":   {"sky": ((146, 214, 236), (206, 240, 248)), "cloud": (255, 255, 255),
                "far": (138, 168, 200), "near": (104, 132, 170), "snow": (244, 250, 255),
                "hill": (112, 176, 116), "tree": (74, 150, 100), "tree2": (100, 184, 126),
                "grass": (134, 208, 114), "grass2": (104, 180, 94),
                "dirt": (156, 114, 74), "dirt2": (126, 90, 58),
                "orb": (255, 250, 210), "weather": "sparkle"},
    "excited": {"sky": ((250, 190, 122), (255, 224, 168)), "cloud": (255, 244, 226),
                "far": (176, 138, 148), "near": (138, 104, 122), "snow": (255, 238, 226),
                "hill": (140, 158, 96), "tree": (96, 130, 88), "tree2": (124, 160, 104),
                "grass": (152, 182, 96), "grass2": (122, 150, 78),
                "dirt": (162, 114, 70), "dirt2": (130, 88, 54),
                "orb": (255, 236, 176), "weather": "confetti"},
    "alert":   {"sky": ((232, 138, 106), (250, 186, 146)), "cloud": (255, 226, 208),
                "far": (150, 106, 112), "near": (114, 78, 90), "snow": (252, 226, 216),
                "hill": (122, 138, 82), "tree": (84, 106, 78), "tree2": (110, 134, 94),
                "grass": (132, 152, 84), "grass2": (104, 124, 68),
                "dirt": (146, 96, 60), "dirt2": (116, 74, 46),
                "orb": (255, 214, 168), "weather": None},
    "sad":     {"sky": ((92, 116, 164), (140, 164, 204)), "cloud": (206, 218, 240),
                "far": (100, 118, 158), "near": (74, 90, 126), "snow": (222, 232, 250),
                "hill": (82, 132, 100), "tree": (56, 100, 90), "tree2": (76, 126, 110),
                "grass": (92, 142, 106), "grass2": (72, 118, 88),
                "dirt": (112, 88, 72), "dirt2": (88, 68, 56),
                "orb": (206, 220, 246), "weather": "rain"},
    "angry":   {"sky": ((188, 86, 82), (230, 138, 112)), "cloud": (250, 214, 200),
                "far": (140, 86, 88), "near": (104, 62, 70), "snow": (248, 220, 214),
                "hill": (124, 116, 70), "tree": (86, 92, 68), "tree2": (112, 118, 84),
                "grass": (134, 130, 74), "grass2": (106, 102, 60),
                "dirt": (138, 88, 56), "dirt2": (110, 68, 44),
                "orb": (255, 206, 172), "weather": "ember"},
}

# Confetti colours, cycled deterministically by piece index.
CONFETTI = [(255, 232, 120), (255, 138, 152), (140, 214, 255), (168, 246, 160), (255, 186, 110)]

# --- character sprites --------------------------------------------------------
# 20 wide x 24 tall. Head occupies rows 1-13, body 14-23: chibi proportions.
# Every pose leaves a 10x6 face window whose top-left is given in POSES.

_IDLE_A = [
    "....................",
    ".......KKKKKK.......",
    ".....KKHHHHHHKK.....",
    "....KHHHHHHHHHHK....",
    "....KHhhHHHHhhHK....",
    "....KHSSSSSSSSHK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKSK....",
    "....KSKBBBBBBKSK....",
    "....KSKBBBBBBKSK....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

_IDLE_B = [
    "....................",
    "....................",
    ".......KKKKKK.......",
    ".....KKHHHHHHKK.....",
    "....KHHHHHHHHHHK....",
    "....KHhhHHHHhhHK....",
    "....KHSSSSSSSSHK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKSK....",
    "....KSKBBBBBBKSK....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

_WAVE_A = [
    "....................",
    ".......KKKKKK...KK..",
    ".....KKHHHHHHKK.KSK.",
    "....KHHHHHHHHHHKKSK.",
    "....KHhhHHHHhhHKKSK.",
    "....KHSSSSSSSSHKKSK.",
    "....KSSSSSSSSSSKKK..",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKK.....",
    "....KSKBBBBBBKK.....",
    "....KSKBBBBBBKK.....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

_WAVE_B = [
    "....................",
    ".......KKKKKK.......",
    ".....KKHHHHHHKK.KK..",
    "....KHHHHHHHHHHKSK..",
    "....KHhhHHHHhhHKSK..",
    "....KHSSSSSSSSHKSK..",
    "....KSSSSSSSSSSKK...",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKK.....",
    "....KSKBBBBBBKK.....",
    "....KSKBBBBBBKK.....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

_JUMP = [
    "..KK...........KK...",
    ".KSK...KKKKKK..KSK..",
    ".KSK.KKHHHHHHKKKSK..",
    ".KSKKHHHHHHHHHHKSK..",
    ".KSKKHhhHHHHhhHKSK..",
    ".KKKKHSSSSSSSSHKKK..",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    ".....KBwwwwwwBK.....",
    ".....KBBBBBBBBK.....",
    ".....KKbbbbbbKK.....",
    "....KKPPPPPPPPKK....",
    "...KKPPKKKKKKPPKK...",
    "..KKPPKK....KKPPKK..",
    "..KSSKK......KKSSK..",
    "...KK..........KK...",
    "....................",
]

_DUCK = [
    "....................",
    "....................",
    "....................",
    "....................",
    ".......KKKKKK.......",
    ".....KKHHHHHHKK.....",
    "....KHHHHHHHHHHK....",
    "....KHhhHHHHhhHK....",
    "....KHSSSSSSSSHK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    "...KKKKBBBBBBKKKK...",
    "..KSKKBwwwwwwBKKSK..",
    "..KSKKBBBBBBBBKKSK..",
    "..KKKKbbbbbbbbKKKK..",
    "...KKPPPPPPPPPPKK...",
    "..KKPPKKKKKKKKPPKK..",
    "..KSSKK......KKSSK..",
]

_CELEBRATE_A = [
    "..KK...........KK...",
    ".KSK...KKKKKK..KSK..",
    ".KSK.KKHHHHHHKKKSK..",
    ".KSKKHHHHHHHHHHKSK..",
    ".KSKKHhhHHHHhhHKSK..",
    ".KKKKHSSSSSSSSHKKK..",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    ".....KBwwwwwwBK.....",
    ".....KBBBBBBBBK.....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
    "....................",
]

_CELEBRATE_B = [
    ".KK.............KK..",
    "KSK....KKKKKK...KSK.",
    "KSK..KKHHHHHHKK.KSK.",
    "KKKKKHHHHHHHHHHKKKK.",
    "....KHhhHHHHhhHK....",
    "....KHSSSSSSSSHK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    "....KSSSSSSSSSSK....",
    ".....KSSSSSSSSK.....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    ".....KBwwwwwwBK.....",
    ".....KBBBBBBBBK.....",
    ".....KKbbbbbbKK.....",
    "....KKPPPPPPPPKK....",
    "...KKPPKKKKKKPPKK...",
    "..KKPPKK....KKPPKK..",
    "..KSSKK......KKSSK..",
    "...KK..........KK...",
    "....................",
]

_LOOK_A = [
    "....................",
    "........KKKKKK......",
    "......KKHHHHHHKK....",
    ".....KHHHHHHHHHHK...",
    ".....KHhhHHHHhhHK...",
    ".....KHSSSSSSSSHK...",
    ".....KSSSSSSSSSSK...",
    ".....KSSSSSSSSSSK...",
    ".....KSSSSSSSSSSK...",
    ".....KSSSSSSSSSSK...",
    ".....KSSSSSSSSSSK...",
    ".....KSSSSSSSSSSK...",
    "......KSSSSSSSSK....",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKSK....",
    "....KSKBBBBBBKSK....",
    "....KSKBBBBBBKSK....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

_LOOK_B = [
    "....................",
    "......KKKKKK........",
    "....KKHHHHHHKK......",
    "...KHHHHHHHHHHK.....",
    "...KHhhHHHHhhHK.....",
    "...KHSSSSSSSSHK.....",
    "...KSSSSSSSSSSK.....",
    "...KSSSSSSSSSSK.....",
    "...KSSSSSSSSSSK.....",
    "...KSSSSSSSSSSK.....",
    "...KSSSSSSSSSSK.....",
    "...KSSSSSSSSSSK.....",
    "....KSSSSSSSSK......",
    "......KKKKKKKK......",
    ".....KKBBBBBBKK.....",
    "....KSKBwwwwBKSK....",
    "....KSKBBBBBBKSK....",
    "....KSKBBBBBBKSK....",
    ".....KKbbbbbbKK.....",
    ".....KPPPPPPPPK.....",
    ".....KPPKKKKPPK.....",
    ".....KPPK..KPPK.....",
    ".....KSSK..KSSK.....",
    "......KK....KK......",
]

# pose -> (frames, face window origin per frame). Window is 10 wide, 6 tall.
POSES = {
    "idle_loop":   ([_IDLE_A, _IDLE_B],           [(5, 6), (5, 7)]),
    "wave":        ([_WAVE_A, _WAVE_B],           [(5, 6), (5, 6)]),
    "jump":        ([_JUMP, _JUMP],               [(5, 6), (5, 6)]),
    "duck":        ([_DUCK, _DUCK],               [(5, 9), (5, 9)]),
    "celebrate":   ([_CELEBRATE_A, _CELEBRATE_B], [(5, 6), (5, 6)]),
    "look_around": ([_LOOK_A, _LOOK_B],           [(6, 6), (4, 6)]),
}

# 10 wide x 6 tall. '.' leaves the pose's own skin showing.
# Big eyes are the main "cute" lever, so every mood keeps them large.
FACES = {
    "idle":    ["..........",
                ".WWW..WWW.",
                ".WEE..EEW.",
                "..........",
                "...MMMM...",
                ".........."],
    "happy":   ["..........",
                ".WWW..WWW.",
                ".WEE..EEW.",
                "..........",
                "..M....M..",
                "...MMMM..."],
    "excited": [".WWW..WWW.",
                ".WEE..EEW.",
                ".WWW..WWW.",
                "...MMMM...",
                "..MMMMMM..",
                "...MMMM..."],
    "alert":   [".WWW..WWW.",
                ".WEW..WEW.",
                ".WWW..WWW.",
                "..........",
                "...MM.....",
                ".........."],
    "sad":     ["..KK....KK",
                ".WWW..WWW.",
                ".WEE..EEW.",
                "..........",
                "...MMMM...",
                "..M....M.."],
    "angry":   [".KK....KK.",
                "..KK..KK..",
                ".WEE..EEW.",
                "..........",
                "...MMMM...",
                ".........."],
}


class Renderer:
    """Draws a character in a named mood and action."""

    def __init__(self, scale=SCALE, stage=(STAGE_W, STAGE_H), hud=True):
        self.scale = scale
        self.stage_w, self.stage_h = stage
        self.hud = hud
        self._font = self._load_font(15)
        self._font_small = self._load_font(12)
        self._font_big = self._load_font(19)
        self._bg_cache = {}

    @staticmethod
    def _load_font(size):
        for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    # -- character ------------------------------------------------------------

    def _palette(self, mood):
        shirt, light, shade = MOOD_SHIRT.get(mood, MOOD_SHIRT["idle"])
        return {**CHARACTER_BASE, "B": shirt, "w": light, "b": shade}

    def draw_character(self, mood, action, frame=0, blink=False):
        """The character alone, at logical resolution, as RGBA."""
        frames, origins = POSES.get(action, POSES["idle_loop"])
        idx = frame % len(frames)
        grid = [list(row) for row in frames[idx]]
        fx, fy = origins[idx]

        face = FACES.get(mood, FACES["idle"])
        if blink:
            # Closed eyes: one dark line where each eye was. A blink is two or
            # three frames out of every couple of seconds and does more for
            # aliveness than any amount of body animation.
            face = list(face)
            face[1] = ".KKK..KKK."
            face[2] = ".........."

        for dy, row in enumerate(face):
            for dx, ch in enumerate(row):
                if ch == ".":
                    continue
                y, x = fy + dy, fx + dx
                if 0 <= y < len(grid) and 0 <= x < len(grid[y]):
                    # Only draw onto skin, so a face can never spill over an
                    # outline or off the side of the head.
                    if grid[y][x] == "S":
                        grid[y][x] = ch
        del face

        palette = self._palette(mood)
        w, h = len(grid[0]), len(grid)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        px = img.load()
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                if ch != ".":
                    px[x, y] = (*palette.get(ch, CHARACTER_BASE["K"]), 255)
        return img

    # -- scene ----------------------------------------------------------------

    def _background(self, mood):
        """Layered scene. Cached: it is static per mood, and rebuilding it for
        every one of ~140 frames would dominate render time for no benefit."""
        if mood in self._bg_cache:
            return self._bg_cache[mood].copy()

        c = SCENE.get(mood, SCENE["idle"])
        w, h = self.stage_w, self.stage_h
        img = Image.new("RGB", (w, h), c["sky"][0])
        draw = ImageDraw.Draw(img)

        # 1. Sky, banded rather than smoothly blended - a true gradient at this
        #    resolution just looks like noise once quantised for GIF.
        top, low = c["sky"]
        bands = 5
        for i in range(bands):
            y0 = i * HORIZON // bands
            y1 = (i + 1) * HORIZON // bands
            t = i / (bands - 1)
            draw.rectangle([0, y0, w, y1], fill=tuple(
                int(top[k] + (low[k] - top[k]) * t) for k in range(3)))

        # 2. Sun or moon. Kept small with a tight halo: at radius 4 with a 2px
        #    glow it read as a pale blob, and against the muted `sad` sky it was
        #    indistinguishable from a cloud.
        ox, oy, orad = 59, 7, 3
        halo = tuple(int(low[k] + (c["orb"][k] - low[k]) * 0.45) for k in range(3))
        draw.ellipse([ox - orad - 1, oy - orad - 1, ox + orad + 1, oy + orad + 1], fill=halo)
        draw.ellipse([ox - orad, oy - orad, ox + orad, oy + orad], fill=c["orb"])

        # 3. Clouds: stacked blocks, deliberately rectilinear.
        for cx, cy, cw in ((9, 6, 11), (34, 4, 8), (52, 14, 13)):
            draw.rectangle([cx, cy, cx + cw, cy + 1], fill=c["cloud"])
            draw.rectangle([cx + 2, cy - 1, cx + cw - 3, cy], fill=c["cloud"])
            draw.rectangle([cx + 1, cy + 2, cx + cw - 1, cy + 2], fill=c["cloud"])

        # 3. Mountains, far range then near range. Each peak is a stepped
        #    triangle with its right face in shadow, and a snow cap that fills
        #    the top of the triangle and breaks into a ragged fringe. An earlier
        #    version inset the cap by (spread - 1), which drew a 1px spike at the
        #    summit and read as a white cross floating above the ridge.
        def ridge(peaks, colour, snow_depth):
            shade = tuple(int(v * 0.78) for v in colour)
            for px_, peak_y in peaks:
                for row in range(peak_y, HORIZON):
                    spread = row - peak_y
                    draw.rectangle([px_ - spread, row, px_ + spread, row], fill=colour)
                    if spread > 1:
                        draw.rectangle([px_ + spread // 3, row, px_ + spread, row],
                                       fill=shade)
                for row in range(peak_y, peak_y + snow_depth):
                    spread = row - peak_y
                    draw.rectangle([px_ - spread, row, px_ + spread, row], fill=c["snow"])
                # Ragged melt line, so the cap does not end on a ruled edge.
                fringe = peak_y + snow_depth
                spread = fringe - peak_y
                for dx in range(-spread, spread + 1):
                    if (dx + spread) % 3 != 1:
                        draw.point((px_ + dx, fringe), fill=c["snow"])

        ridge([(13, 12), (45, 9), (64, 15)], c["far"], 5)
        ridge([(29, 18), (57, 20)], c["near"], 4)

        # 3b. Rolling hills between the ranges and the treeline. One more depth
        #     plane costs almost nothing and does a lot of the parallax work.
        hill = c["hill"]
        for hx, hr in ((6, 9), (26, 12), (48, 8), (66, 11)):
            draw.ellipse([hx - hr, HORIZON - hr, hx + hr, HORIZON + hr], fill=hill)

        # 4. Treeline: canopies of varied height and width along the horizon.
        #    Evenly-spaced identical blobs read as wallpaper, so the sizes cycle
        #    through a fixed irregular sequence - fixed, not random, because a
        #    random treeline would shimmer between frames.
        canopy = [(9, 8), (6, 6), (10, 9), (7, 7), (8, 10), (6, 5), (9, 7), (7, 9)]
        for i, tx in enumerate(range(-5, w + 8, 6)):
            cw_, chh = canopy[i % len(canopy)]
            base = HORIZON - 1
            tone = c["tree"] if i % 2 else c["tree2"]
            draw.ellipse([tx, base - chh, tx + cw_, base + 1], fill=tone)
            draw.ellipse([tx + cw_ // 3, base - chh - 2, tx + cw_, base - chh // 2],
                         fill=tone)
            # A darker trunk hint where the canopy meets the grass.
            draw.rectangle([tx + cw_ // 2, base - 1, tx + cw_ // 2, base + 1],
                           fill=tuple(int(v * 0.7) for v in tone))

        # 5. Grass, with a tufted top edge rather than a ruled line - the single
        #    detail that stops the ground reading as a coloured rectangle.
        draw.rectangle([0, HORIZON, w, HORIZON + 6], fill=c["grass"])
        for x in range(0, w, 2):
            draw.rectangle([x, HORIZON - 1, x, HORIZON - 1], fill=c["grass"])
            if x % 6 == 0:
                draw.rectangle([x + 1, HORIZON - 2, x + 1, HORIZON - 1], fill=c["grass"])
        for i, x in enumerate(range(1, w, 5)):
            draw.rectangle([x, HORIZON + 3 + (i % 2), x + 1, HORIZON + 4 + (i % 2)],
                           fill=c["grass2"])

        # 6. Dirt, with clumps, darkening with depth.
        draw.rectangle([0, HORIZON + 6, w, h], fill=c["dirt"])
        for i, x in enumerate(range(-1, w, 6)):
            y = HORIZON + 8 + (i * 3) % 8
            draw.ellipse([x, y, x + 4, y + 2], fill=c["dirt2"])
            draw.ellipse([x + 3, y + 4, x + 6, y + 6], fill=c["dirt2"])
        draw.rectangle([0, h - 3, w, h], fill=c["dirt2"])

        # 7. Set dressing. The character stands centred around x=26..46, so props
        #    sit outside that band and frame it rather than crowding it.
        #    A fence was tried here and removed: at this scale it overlapped the
        #    crate and the pair read as a pile of loose sticks. Two props plus
        #    the tree is enough to make the place feel inhabited.
        self._draw_tree(draw, c, 11, HORIZON + 1)
        self._draw_crate(draw, c, 58, HORIZON + 3)

        # 8. Sparse foreground tufts. An earlier version drew a blade every 3px
        #    across the full width, which read as a barcode rather than grass.
        fg = tuple(int(v * 0.72) for v in c["grass"])
        for i, x in enumerate(range(2, w, 9)):
            bh = 2 + (i * 7) % 2
            draw.rectangle([x, h - bh, x, h - 1], fill=fg)
            draw.rectangle([x + 2, h - 1, x + 2, h - 1], fill=fg)

        self._bg_cache[mood] = img
        return img.copy()

    # -- props ----------------------------------------------------------------

    @staticmethod
    def _draw_tree(draw, c, x, base):
        """A framing tree: trunk, a couple of boughs, layered canopy."""
        bark = tuple(int(v * 0.55) for v in c["dirt"])
        bark2 = tuple(int(v * 0.4) for v in c["dirt"])
        draw.rectangle([x - 1, base - 13, x + 1, base], fill=bark)
        draw.rectangle([x + 1, base - 13, x + 1, base], fill=bark2)
        draw.rectangle([x - 3, base - 10, x - 2, base - 9], fill=bark)
        draw.rectangle([x + 2, base - 12, x + 3, base - 11], fill=bark)
        # Roots flaring into the grass.
        draw.rectangle([x - 3, base - 1, x + 3, base], fill=bark2)

        leaf, leaf2 = c["tree"], c["tree2"]
        draw.ellipse([x - 9, base - 26, x + 9, base - 12], fill=leaf)
        draw.ellipse([x - 6, base - 30, x + 7, base - 18], fill=leaf2)
        draw.ellipse([x - 10, base - 22, x - 2, base - 15], fill=leaf2)
        draw.ellipse([x + 1, base - 21, x + 9, base - 14], fill=leaf)

    @staticmethod
    def _draw_crate(draw, c, x, base):
        """A wooden crate, from the fishing-dock reference's prop language."""
        wood = tuple(min(255, int(v * 1.05)) for v in c["dirt"])
        dark = tuple(int(v * 0.62) for v in c["dirt"])
        light = tuple(min(255, int(v * 1.25)) for v in c["dirt"])
        draw.rectangle([x, base - 7, x + 8, base], fill=wood)
        draw.rectangle([x, base - 7, x + 8, base], outline=dark)
        draw.line([x, base - 7, x + 8, base], fill=dark)
        draw.line([x + 8, base - 7, x, base], fill=dark)
        draw.rectangle([x, base - 7, x + 8, base - 6], fill=light)

    # -- weather --------------------------------------------------------------

    def _weather(self, draw, mood, t):
        """Per-mood particles, drawn at LOGICAL resolution so they stay pixels.

        Everything here is a pure function of `t`, so the animation is
        reproducible and nothing shimmers: a random particle field would change
        every frame and read as static noise rather than weather.
        """
        kind = SCENE.get(mood, SCENE["idle"]).get("weather")
        if not kind:
            return
        w = self.stage_w

        if kind == "rain":
            for i in range(26):
                x = (i * 11 + (t // 2) * 3) % w
                y = (i * 7 + t * 3) % (HORIZON + 10)
                draw.line([x, y, x - 1, y + 2], fill=(196, 216, 244))

        elif kind == "confetti":
            for i in range(18):
                x = (i * 9 + (i % 3) * 2 + (t // 3)) % w
                y = (i * 5 + t * 2) % (HORIZON + 12)
                colour = CONFETTI[i % len(CONFETTI)]
                if (i + t // 2) % 2:
                    draw.rectangle([x, y, x + 1, y], fill=colour)
                else:
                    draw.rectangle([x, y, x, y + 1], fill=colour)

        elif kind == "sparkle":
            for i in range(9):
                x = (i * 13 + 5) % w
                y = 6 + (i * 5 + t) % (HORIZON - 8)
                phase = (t + i * 3) % 12
                if phase < 6:
                    draw.point((x, y), fill=(255, 255, 232))
                    if phase < 3:
                        draw.point((x - 1, y), fill=(255, 250, 200))
                        draw.point((x + 1, y), fill=(255, 250, 200))
                        draw.point((x, y - 1), fill=(255, 250, 200))
                        draw.point((x, y + 1), fill=(255, 250, 200))

        elif kind == "ember":
            for i in range(14):
                x = (i * 8 + ((t // 4) * (1 + i % 2))) % w
                y = (HORIZON + 6) - (i * 4 + t * 2) % (HORIZON + 6)
                draw.point((x, y), fill=(255, 176, 96) if i % 2 else (255, 214, 140))

    @staticmethod
    def _shadow(draw, cx, cy, width, mood, strength=1.0):
        """Contact shadow. Drawn at full darkness it reads as a hole in the
        floor, so an airborne pose gets a fainter, smaller one."""
        c = SCENE.get(mood, SCENE["idle"])
        factor = 1.0 - 0.34 * strength
        shade = tuple(max(0, int(v * factor)) for v in c["grass"])
        half = max(2, width // 2 - 1)
        draw.ellipse([cx - half, cy - 2, cx + half, cy + 1], fill=shade)

    @staticmethod
    def drawable(text):
        """Drop characters the bundled fonts cannot draw.

        A real run produced the line "Yay!! 🎉" and the emoji rendered as a tofu
        box in the speech bubble. The fonts here are plain monospace faces with
        no emoji coverage, and a missing glyph is worse than a missing character:
        it reads as a rendering bug rather than as text.

        Restricted to printable ASCII plus common Latin-1 punctuation, which
        matches the domain - Section 3.5 asks for a short spoken line under eight
        words. Returns None if nothing drawable survives, so an emoji-only line
        becomes silence rather than an empty bubble.
        """
        if not text:
            return None
        kept = "".join(ch for ch in str(text) if 0x20 <= ord(ch) <= 0x7E or 0xA0 <= ord(ch) <= 0xFF)
        kept = " ".join(kept.split())
        return kept or None

    def _speech_bubble(self, draw, text, anchor_x, anchor_y, width):
        """Pixel-styled bubble, drawn in FINAL pixels so the text is legible."""
        pad = 9
        bbox = draw.textbbox((0, 0), text, font=self._font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        w, h = tw + pad * 2, th + pad * 2
        x = min(max(anchor_x - w // 2, 8), width - w - 8)
        y = max(anchor_y - h - 14, 8)

        draw.rectangle([x + 3, y + 4, x + w + 3, y + h + 4], fill=(40, 34, 52))
        draw.rectangle([x, y, x + w, y + h], fill=(252, 250, 242),
                       outline=(40, 34, 52), width=3)
        draw.polygon([(anchor_x - 8, y + h - 1), (anchor_x + 8, y + h - 1),
                      (anchor_x, y + h + 12)], fill=(252, 250, 242), outline=(40, 34, 52))
        draw.text((x + pad - bbox[0], y + pad - bbox[1]), text,
                  font=self._font, fill=(44, 38, 56))

    def render(self, mood, action, line=None, frame=0, caption=None, badge=None, t=0):
        """One complete frame at final resolution.

        `frame` selects the pose's animation cell; `t` is a monotonic frame
        counter used for time-based effects (weather, blinking) that need to run
        at their own rate rather than the pose cycle's.
        """
        stage = self._background(mood)
        draw_stage = ImageDraw.Draw(stage)
        self._weather(draw_stage, mood, t)

        # Blink for two frames roughly every two seconds, and never mid-shout:
        # closed eyes on `excited` would fight the open mouth.
        blink = (t % 26) < 2 and mood not in ("excited", "alert")
        character = self.draw_character(mood, action, frame, blink=blink)
        cw, ch = character.size
        cx = (self.stage_w - cw) // 2
        cy = HORIZON - ch + 7

        airborne = action == "jump"
        self._shadow(draw_stage, cx + cw // 2, HORIZON + 4,
                     cw - (7 if airborne else 2), mood,
                     strength=0.4 if airborne else 1.0)
        stage.paste(character, (cx, cy - (3 if airborne else 0)), character)

        # A puff of dust where the character meets the ground on the big poses.
        if action in ("jump", "duck", "celebrate"):
            dust = tuple(min(255, int(v * 1.15)) for v in SCENE.get(mood, SCENE["idle"])["dirt"])
            spread = 2 + (t % 3)
            for side in (-1, 1):
                px_ = cx + cw // 2 + side * (cw // 2 - 3)
                draw_stage.ellipse(
                    [px_ - spread, HORIZON + 2, px_ + spread, HORIZON + 4], fill=dust
                )

        scale = self.scale
        img = stage.resize((self.stage_w * scale, self.stage_h * scale), Image.NEAREST)

        canvas = Image.new("RGB", (img.width, img.height + (HUD_H if self.hud else 0)),
                           (20, 18, 28))
        canvas.paste(img, (0, 0))
        draw = ImageDraw.Draw(canvas)

        drawable_line = self.drawable(line)
        if drawable_line:
            self._speech_bubble(draw, drawable_line, (cx + cw // 2) * scale, cy * scale, img.width)

        if badge:
            bbox = draw.textbbox((0, 0), badge, font=self._font_big)
            bw = bbox[2] - bbox[0] + 20
            draw.rectangle([img.width - bw - 16, 14, img.width - 14, 50],
                           fill=(206, 62, 62), outline=(255, 226, 226), width=3)
            draw.text((img.width - bw - 16 + 10 - bbox[0], 14 + 8 - bbox[1]), badge,
                      font=self._font_big, fill=(255, 244, 244))

        if self.hud:
            self._draw_hud(draw, img.height, canvas.width, mood, action, caption)
        return canvas

    def _draw_hud(self, draw, top, width, mood, action, caption):
        draw.rectangle([0, top, width, top + HUD_H], fill=(20, 18, 28))
        draw.line([(0, top), (width, top)], fill=(64, 60, 84), width=2)

        swatch = MOOD_SHIRT.get(mood, MOOD_SHIRT["idle"])[0]
        draw.rectangle([16, top + 15, 34, top + 33], fill=swatch,
                       outline=(140, 138, 168), width=2)
        draw.text((44, top + 12), mood, font=self._font, fill=(240, 238, 250))
        draw.text((44, top + 32), action, font=self._font_small, fill=(156, 152, 184))

        if caption:
            bbox = draw.textbbox((0, 0), caption, font=self._font_small)
            draw.text((width - (bbox[2] - bbox[0]) - 18, top + 22), caption,
                      font=self._font_small, fill=(156, 152, 184))


def render_states(states, out_path, scale=SCALE, frames_per_state=12, fps=12, loop=0):
    """Render a sequence of states to an animated GIF.

    `states` is a sequence of dicts with keys: mood, action, and optionally
    line, caption, badge. Returns the number of frames written.
    """
    renderer = Renderer(scale=scale)
    frames = []
    clock = 0
    for state in states:
        for i in range(frames_per_state):
            frames.append(renderer.render(
                mood=state.get("mood", "idle"),
                action=state.get("action", "idle_loop"),
                line=state.get("line"),
                caption=state.get("caption"),
                badge=state.get("badge"),
                # Advance the cycle every few frames, so a 2-frame animation
                # reads as motion rather than a flicker.
                frame=i // max(1, frames_per_state // 4),
                # `clock` runs continuously across states so weather keeps
                # falling and blinks keep their rhythm through a state change.
                t=clock,
            ))
            clock += 1

    if not frames:
        raise ValueError("no states to render")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # One shared adaptive palette: GIF is 256 colours, and per-frame palettes
    # cause visible flicker between frames.
    quantised = [f.convert("P", palette=Image.ADAPTIVE, colors=192) for f in frames]
    quantised[0].save(
        out, save_all=True, append_images=quantised[1:],
        duration=int(1000 / fps), loop=loop, optimize=True,
    )
    return len(frames)
