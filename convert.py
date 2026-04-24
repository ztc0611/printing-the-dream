#!/usr/bin/env python3
"""
convert.py — turn a PNG into a macro that draws it in the Tomodachi Life item
creator. Output runs on the Pico CircuitPython build in pico/.
Color-grouped TSP-ordered path, multi-brush, skips A-press on transparent
pixels.

Usage:
    python3 convert.py <image.png>

Outputs (default):
    <name>_macro.mz          binary macro for the Pico
    <name>_macro.bmp         72×72 thumbnail shown by the Pico UI
    <name>_preview.png       full-res palette-snap preview (not for the Pico)

Additional outputs with --debug:
    <name>_simulated.png     dry-run trace of what the macro will draw
    <name>_macro_info.json   generation stats + starting-state assumptions

Assumes when the macro starts:
  - Canvas cursor is at (128, 128) (canvas center) with the 1x1 brush.
  - Palette cursor is at index 72 (pure black, bottom-left). True on a fresh
    item file before any color is changed.

Macro convention per pixel:
  - Move from current canvas position to the target pixel.
  - If color differs from current: open palette (Y, Y), navigate from previous
    palette cell to target cell, press A to confirm + return to drawing.
  - Press A to draw the pixel.
"""
from pathlib import Path
import argparse
import json

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

# Canvas tap cadence — 34/33ms is the empirical reliable floor.
PRESS = 0.034
PAUSE = 0.033
# Pre-A and post-A transitions around the canvas A-stamp.
PRE_A_PAUSE = 0.033
A_PAUSE = 0.033
# STAMP combines the last move step with the A press into a single HID report
# so the stamp lands on the destination cell — saves one press cycle per pixel.
STAMP_PRESS = 0.034
STAMP_PAUSE = 0.033
# Palette nav runs at the same cadence as canvas; the grid-open animation
# (POST_PALETTE_OPEN) is what needs buffering, not the tap cadence.
PALETTE_PRESS = 0.067
PALETTE_PAUSE = 0.133
# Brush menu runs slower: a dropped A on brush commit leaves the wrong brush
# active, and 3x3 stamps afterward degrade to 1x1 (holes in solid fills).
# Rare (~30 switches/print) so overkill here is cheap insurance.
BRUSH_PRESS = 0.1
BRUSH_PAUSE = 0.2
# Gap between the two Y presses — Switch UI transitions from the 10-slot
# sidebar to the full palette grid.
Y_GAP = 0.15
# Palette grid's open animation eats d-pad inputs for a few frames. Without
# this buffer the first palette move drops and every color lands off-by-one.
POST_PALETTE_OPEN = 0.3
# After confirm-A on the palette grid the close animation eats canvas d-pad
# inputs; wait it out before resuming movement.
POST_PALETTE_GAP = 1.0
# Per-press wall-clock overhead not captured by duration tokens (USB HID poll
# boundary wait on the Switch's 8ms cycle).
PRESS_OVERHEAD = 0.0042
PALETTE_COLS = 12
PALETTE_ROWS = 7
ALPHA_THRESHOLD = 128
INITIAL_PALETTE_IDX = 72  # bottom-left = pure black on a fresh file

# Brush selector menu: X,X opens it (analogous to palette's Y,Y). Grid is
# 3 cols × 2 rows; positions are (col, row). Top row is circle brushes, bottom
# row is square brushes. The top-row 1x1 and bottom-row 1x1 behave identically;
# we prefer the bottom-row 1x1 so switches to 3x3 are a single right-tap.
# "null" is the top-right circle (too big to use) but is where the brush cursor
# sits on a fresh item file — we navigate off it at macro start.
#
#    col 0   col 1   col 2
#  row 0: (1x1)   plus    null  <- fresh-file cursor
#  row 1:  1x1    3x3     5x5
BRUSH_POSITIONS: dict[str, tuple[int, int]] = {
    "null": (2, 0),
    "plus": (1, 0),
    "1x1":  (0, 1),
    "3x3":  (1, 1),
}
BRUSH_PATTERNS: dict[str, list[tuple[int, int]]] = {
    "1x1":  [(0, 0)],
    "3x3":  [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)],
    "plus": [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)],
}
INITIAL_BRUSH = "1x1"  # post-preamble state; macro_runner selects 1x1 from null
# Minimum tiles a non-1x1 brush must place to cover its switch cost (enter+exit
# ≈ 4s; per-tile savings: 3x3=1.6s, +=0.8s). Under-threshold groups get demoted
# back to 1x1. Module-level so the first-brush peek can mirror the same logic.
MIN_TILES = {"3x3": 3, "plus": 5}


def load_palette(path: Path) -> np.ndarray:
    return np.array(
        [d["rgb"] for d in json.loads(path.read_text())],
        dtype=np.float32,
    )


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    """sRGB gamma decode. Input/output in [0, 1]."""
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """Björn Ottosson's OKLab transform. Input: linear RGB in [0,1],
    shape (..., 3). Output: Lab in the same leading shape.

    OKLab is perceptually uniform, so Euclidean distance there matches
    how humans actually see color similarity — unlike sRGB-space
    Euclidean which picks brownish bridge colors for saturated
    gradients."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_ = np.cbrt(l); m_ = np.cbrt(m); s_ = np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, A, B], axis=-1)


def _srgb_to_oklab(rgb_0_255: np.ndarray) -> np.ndarray:
    return _linear_rgb_to_oklab(_srgb_to_linear(rgb_0_255.astype(np.float32) / 255.0))


def quantize(img: Image.Image, palette: np.ndarray, use_oklab: bool = False):
    """Snap each pixel to the nearest palette color.

    Default metric: Euclidean distance in sRGB — fast but picks muddy
    brown bridges on saturated gradients. `use_oklab=True` switches to
    perceptually-uniform OKLab distance which picks more vibrant hues
    but often balloons color switches (and therefore runtime) because
    the image ends up using more distinct palette entries."""
    arr = np.array(img.convert("RGBA"))
    rgb = arr[..., :3].reshape(-1, 3)
    alpha = arr[..., 3]
    if use_oklab:
        p = _srgb_to_oklab(rgb)
        q = _srgb_to_oklab(palette)
    else:
        p = rgb.astype(np.float32)
        q = palette.astype(np.float32)
    dists = np.linalg.norm(p[:, None, :] - q[None, :, :], axis=2)
    indices = np.argmin(dists, axis=1).reshape(arr.shape[:2])
    mask = alpha >= ALPHA_THRESHOLD
    return indices, mask


# Candidate brush sets evaluated independently for each color when per_color=True.
# Mirrors the global auto-picker's candidate list so any option the global picker
# could choose, the per-color picker can choose for any individual color.
_PER_COLOR_CANDIDATES = (
    ("3x3", "1x1"),
    ("plus", "1x1"),
    ("3x3", "plus", "1x1"),
    ("1x1",),
)


def _eval_brush_set(start, own, overstampable, brush_set, prior_brush):
    """Estimate wall-clock cost of painting `own` with a given brush priority.
    Returns (cost_seconds, end_cursor, end_brush). Matches the real emit-path
    tiling (tile_color_pixels + MIN_TILES demotion) and a greedy-NN tour for
    travel — no 2-opt, to keep per-candidate-per-color evaluation fast."""
    by_brush = tile_color_pixels(own, overstampable, list(brush_set))
    for b in list(by_brush):
        if b == "1x1":
            continue
        if len(by_brush[b]) < MIN_TILES.get(b, 1):
            del by_brush[b]
    stamp_s = STAMP_PRESS + STAMP_PAUSE + PRESS_OVERHEAD
    travel_s = PRESS + PAUSE + PRESS_OVERHEAD
    brush_switch_s = 4.0
    cur = start
    total = 0.0
    cur_brush = prior_brush
    for b in brush_set:
        anchors = by_brush.get(b, [])
        if not anchors:
            continue
        if b != cur_brush:
            total += brush_switch_s
            cur_brush = b
        pts = np.asarray(anchors, dtype=np.int32)
        n = len(pts)
        visited = np.zeros(n, dtype=bool)
        INF = np.iinfo(np.int32).max
        cur_x, cur_y = np.int32(cur[0]), np.int32(cur[1])
        travel_cells = 0
        for _ in range(n):
            d = np.maximum(np.abs(pts[:, 0] - cur_x),
                           np.abs(pts[:, 1] - cur_y))
            d[visited] = INF
            k = int(d.argmin())
            travel_cells += int(d[k])
            visited[k] = True
            cur_x, cur_y = pts[k, 0], pts[k, 1]
        cur = (int(cur_x), int(cur_y))
        total += travel_cells * travel_s
        total += n * stamp_s
    return total, cur, cur_brush


def _pick_per_color_brushes(pixels_by_color, color_order, later):
    """For each color, evaluate every candidate brush set and keep the
    cheapest. Returns a list parallel to color_order — each entry is the brush
    priority list to use for that color's tiling. Threads cursor + current
    brush state through so each pick sees the realistic incoming state."""
    cx, cy = 128, 128
    cur_brush = INITIAL_BRUSH
    picks: list[list[str]] = []
    for idx, color in enumerate(color_order):
        own = pixels_by_color[color]
        overstampable = set(own) | later[idx]
        best = None
        for cs in _PER_COLOR_CANDIDATES:
            cost, end_cur, end_brush = _eval_brush_set(
                (cx, cy), own, overstampable, cs, cur_brush)
            if best is None or cost < best[0]:
                best = (cost, end_cur, end_brush, list(cs))
        picks.append(best[3])
        cx, cy = best[1]
        cur_brush = best[2]
    return picks


def _first_used_brush(pixels_by_color, color_order, brushes):
    """Predict which brush will be active at the first stamp so macro_runner's
    preamble can land on that brush and skip the first brush-menu cycle. The
    logic mirrors the main emit loop's `by_brush` filtering exactly — a
    mismatch would leave the real brush state inconsistent with what the
    macro thinks `current_brush` is."""
    if not color_order:
        return INITIAL_BRUSH
    first = color_order[0]
    own = pixels_by_color[first]
    later_first = set()
    for c in color_order[1:]:
        later_first.update(pixels_by_color[c])
    overstampable = set(own) | later_first
    by_brush = tile_color_pixels(own, overstampable, brushes)
    own_set = set(own)
    for brush in list(by_brush):
        if brush == "1x1":
            continue
        if len(by_brush[brush]) < MIN_TILES.get(brush, 1):
            del by_brush[brush]
    for brush in brushes:
        if by_brush.get(brush):
            return brush
    return INITIAL_BRUSH


class MacroBuilder:
    def __init__(self, initial_brush: str = INITIAL_BRUSH) -> None:
        self.lines: list[str] = []
        self.presses = 0
        self.current_brush = initial_brush
        self.initial_brush = initial_brush
        self.brush_switches = 0

    def brush_select(self, name: str) -> None:
        """Switch to a different brush via the X,X menu. No-op if already set.

        Unlike the palette (where A both selects and closes), the brush menu
        requires B to dismiss — A alone leaves the picker open. The grid-open
        animation eats the first d-pad input after the second X, same as
        palette, so insert POST_PALETTE_OPEN before moving.
        """
        if name == self.current_brush:
            return
        cur_bx, cur_by = BRUSH_POSITIONS[self.current_brush]
        bx, by = BRUSH_POSITIONS[name]
        self.press("X", BRUSH_PRESS, BRUSH_PAUSE)
        # BRUSH_PAUSE already covers the inter-X gap — no extra delay needed.
        self.press("X", BRUSH_PRESS, BRUSH_PAUSE)
        self.lines.append(f"{POST_PALETTE_OPEN}s")
        self.move(bx - cur_bx, by - cur_by, BRUSH_PRESS, BRUSH_PAUSE,
                  diagonal=False)
        self.press("A", BRUSH_PRESS, BRUSH_PAUSE)
        # Post-select animation has to finish before B or the menu doesn't
        # dismiss cleanly.
        self.lines.append(f"{POST_PALETTE_OPEN}s")
        self.press("B", BRUSH_PRESS, BRUSH_PAUSE)
        self.lines.append(f"{POST_PALETTE_GAP}s")
        self.current_brush = name
        self.brush_switches += 1

    def press(self, btn: str, press: float | None = None, pause: float | None = None) -> None:
        p = PRESS if press is None else press
        q = PAUSE if pause is None else pause
        self.lines.append(f"{btn} {p}s")
        self.lines.append(f"{q}s")
        self.presses += 1

    def repeat(self, btn: str, n: int, press: float | None = None, pause: float | None = None) -> None:
        for _ in range(n):
            self.press(btn, press, pause)

    def pen_to(self, dx: int, dy: int) -> None:
        """Move (dx, dy) from current canvas position and stamp at destination.
        Uses STAMP for the final step — combines d-pad + A in one HID report so
        the stamp lands on the destination cell."""
        if dx == 0 and dy == 0:
            self.press("A", pause=A_PAUSE)
            return
        last_dx = (1 if dx > 0 else -1) if dx else 0
        last_dy = (1 if dy > 0 else -1) if dy else 0
        pre_dx = dx - last_dx
        pre_dy = dy - last_dy
        if pre_dx or pre_dy:
            self.move(pre_dx, pre_dy, end_pause=PRE_A_PAUSE)
        if last_dx and last_dy:
            v = "DOWN" if last_dy > 0 else "UP"
            h = "RIGHT" if last_dx > 0 else "LEFT"
            tok = f"DPAD_{v}_{h}"
        elif last_dx:
            tok = "DPAD_RIGHT" if last_dx > 0 else "DPAD_LEFT"
        else:
            tok = "DPAD_DOWN" if last_dy > 0 else "DPAD_UP"
        self.lines.append(f"STAMP_{tok} {STAMP_PRESS}s")
        self.lines.append(f"{STAMP_PAUSE}s")
        self.presses += 1

    def move(
        self, dx: int, dy: int,
        press: float | None = None, pause: float | None = None,
        diagonal: bool = True, end_pause: float | None = None,
    ) -> None:
        press = PRESS if press is None else press
        pause = PAUSE if pause is None else pause
        start_n = len(self.lines)
        # Diagonal hat taps halve presses when both axes need to move — but
        # the palette grid in Tomodachi ignores diagonal hat inputs, so callers
        # navigating the palette must pass diagonal=False.
        diag = min(abs(dx), abs(dy)) if diagonal else 0
        if diag:
            vert = "DOWN" if dy > 0 else "UP"
            horiz = "RIGHT" if dx > 0 else "LEFT"
            self.repeat(f"DPAD_{vert}_{horiz}", diag, press, pause)
        rem_x = abs(dx) - diag
        rem_y = abs(dy) - diag
        if rem_x:
            self.repeat("DPAD_RIGHT" if dx > 0 else "DPAD_LEFT", rem_x, press, pause)
        if rem_y:
            self.repeat("DPAD_DOWN" if dy > 0 else "DPAD_UP", rem_y, press, pause)
        # Override the final pause — lets callers request a tight transition
        # into the next op (e.g. the A-stamp that follows a canvas move).
        if end_pause is not None and len(self.lines) > start_n:
            self.lines[-1] = f"{end_pause}s"


def palette_xy(idx: int) -> tuple[int, int]:
    return idx % PALETTE_COLS, idx // PALETTE_COLS


def tile_color_pixels(
    own_pixels: list[tuple[int, int]],
    overstampable: set[tuple[int, int]],
    brushes_priority: list[str],
) -> dict[str, list[tuple[int, int]]]:
    """Greedy-tile one color's pixels with the brushes in priority order.

    A brush tile for this color is valid if every one of its pattern cells is
    in `overstampable` — which should be (this color's pixels) ∪ (pixels of
    any colors drawn LATER in the schedule). Overstamping later-color cells is
    free: those cells get repainted to their final color in a later pass, so
    the "temporary" stamp costs nothing extra. This lets a dominant background
    color use 3x3 freely through small-feature interruptions.

    Every placed tile must still paint at least one of `own_pixels` (else it
    would do zero useful work). Subsequent tiles of this color may overlap
    cells already painted by a previous same-color tile — re-stamping own-color
    cells is a visual no-op and lets 3x3s fill 1px gaps between earlier 3x3s.

    Returns {brush_name: [anchor, ...]}. Anchors are cursor positions for A.
    """
    remaining = set(own_pixels)
    claimable = set(overstampable)
    out: dict[str, list[tuple[int, int]]] = {}
    for brush in brushes_priority:
        if brush == "1x1":
            continue  # handled at end with whatever's left
        pattern = BRUSH_PATTERNS[brush]
        anchors: list[tuple[int, int]] = []
        # Try every own-pixel as a potential anchor. Row-major for determinism.
        for ax, ay in sorted(own_pixels, key=lambda p: (p[1], p[0])):
            cells = [(ax + dx, ay + dy) for dx, dy in pattern]
            if not all(c in claimable for c in cells):
                continue
            # Must paint enough still-needed own cells to beat the 1x1 cleanup
            # cost. ≥3 new cells per tile is the empirical sweet spot on dense
            # images — lower thresholds over-place overlapping tiles, higher
            # ones miss gap-fill opportunities.
            new_cells = sum(1 for c in cells if c in remaining)
            if new_cells < 3:
                continue
            anchors.append((ax, ay))
            for c in cells:
                remaining.discard(c)
        if anchors:
            out[brush] = anchors
    if remaining:
        out["1x1"] = list(remaining)
    return out


def simulate_macro(
    macro_lines: list[str], size: int, palette: np.ndarray,
    initial_brush: str = INITIAL_BRUSH,
) -> np.ndarray:
    """Replay the macro against a fresh item-file state and return the RGBA
    image it would leave on the canvas. Modes:
      canvas         — DPAD moves cx/cy; A/STAMP paints current_brush at (cx,cy)
      palette_side   — Y opens the 10-slot sidebar first
      palette_grid   — second Y opens the full 12×7 grid; DPAD moves px/py,
                       A commits the new color and returns to canvas.
      brush_side     — X opens the brush sidebar
      brush_grid     — second X opens the full brush grid; DPAD moves bx/by,
                       A commits the brush and returns to canvas.
    """
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    # Match the generator: cursor parked at canvas center (128, 128) after the
    # Pico prelude; first move on the generator side walks it back to (0, 0)
    # before the first stamp. Out-of-bounds stamps are dropped by stamp_at().
    cx, cy = 128, 128
    px, py = palette_xy(INITIAL_PALETTE_IDX)
    color = INITIAL_PALETTE_IDX
    brush = initial_brush
    bx, by = BRUSH_POSITIONS[brush]
    mode = "canvas"

    def stamp_at(ax: int, ay: int) -> None:
        for dx, dy in BRUSH_PATTERNS[brush]:
            nx, ny = ax + dx, ay + dy
            if 0 <= nx < size and 0 <= ny < size:
                canvas[ny, nx, :3] = palette[color].astype(np.uint8)
                canvas[ny, nx, 3] = 255

    for line in macro_lines:
        toks = line.strip().split()
        # Token lines end with a duration like "0.067s" — skip bare pauses.
        if len(toks) < 2 or not toks[-1].endswith("s"):
            continue
        btn = toks[0]
        if btn == "Y":
            mode = "palette_side" if mode == "canvas" else "palette_grid"
        elif btn == "X":
            mode = "brush_side" if mode == "canvas" else "brush_grid"
        elif btn == "A":
            if mode == "canvas":
                stamp_at(cx, cy)
            elif mode == "palette_grid":
                color = py * PALETTE_COLS + px
                mode = "canvas"
            elif mode == "brush_grid":
                # Match brush whose position equals (bx, by). If multiple
                # entries share a position (e.g. 5x5 placeholder), any is fine
                # — generator won't emit for those cases anyway.
                for name, pos in BRUSH_POSITIONS.items():
                    if pos == (bx, by):
                        brush = name
                        break
                mode = "canvas"
        elif btn.startswith("DPAD_"):
            dirs = btn.split("_", 1)[1]
            dx = (1 if "RIGHT" in dirs else 0) - (1 if "LEFT" in dirs else 0)
            dy = (1 if "DOWN" in dirs else 0) - (1 if "UP" in dirs else 0)
            if mode == "canvas":
                cx += dx; cy += dy
            elif mode == "palette_grid":
                # Palette ignores diagonal hat inputs — model it as no-op so
                # any accidental diagonal in palette nav shows up as a diff.
                if dx and dy:
                    pass
                else:
                    px += dx; py += dy
            elif mode == "brush_grid":
                # Assume brush grid also cardinal-only (same pattern as palette).
                if dx and dy:
                    pass
                else:
                    bx += dx; by += dy
        elif btn.startswith("STAMP_DPAD_"):
            dirs = btn.split("_", 2)[2]
            dx = (1 if "RIGHT" in dirs else 0) - (1 if "LEFT" in dirs else 0)
            dy = (1 if "DOWN" in dirs else 0) - (1 if "UP" in dirs else 0)
            if mode == "canvas":
                cx += dx; cy += dy
                stamp_at(cx, cy)
    return canvas


# Binary v3 format — emitted uncompressed as `<name>_macro.mz`. Runner
# stream-reads a byte at a time (zlib.decompress blew the Pico's heap on
# larger macros). Opcode byte layout:
#   byte & 0xE0:
#     0x00  single press      low 5 bits = token idx (0-19)
#     0x20  repeat press      low 5 bits = idx; NEXT byte = count (1-255)
#     0x80  pause marker      NEXT 2 bytes big-endian = pause ms (0-65535)
# All other opcode bytes are reserved. Token-index order = V3_TOKENS below.
V3_TOKENS = [
    "R", "L", "U", "D", "UR", "UL", "DR", "DL",
    "r", "l", "u", "d", "ur", "ul", "dr", "dl",
    "A", "B", "X", "Y",
]
V3_IDX = {t: i for i, t in enumerate(V3_TOKENS)}
V3_OP_SINGLE = 0x00
V3_OP_REPEAT = 0x20
# Long-press: [0x40 | idx] [ms]. Durations up to 255ms. Brush/palette A-presses
# need to hold longer than the 34ms STAMP cadence — a dropped brush-menu A
# leaves the real brush on the previous value, silently degrading 3x3 → 1x1
# for the rest of that pass. SINGLE/REPEAT don't encode a duration byte; this
# opcode does.
V3_OP_LONG_PRESS = 0x40
V3_OP_PAUSE = 0x80


def to_binary_v3(v2_lines: list[str]) -> bytes:
    """Pack v2 compact lines into the binary v3 byte stream (uncompressed)."""
    buf = bytearray()
    for line in v2_lines:
        line = line.strip()
        if not line:
            continue
        if line[0].isdigit():
            ms = int(line)
            # Split into multiple pauses if > 65535ms (65s). Not expected in
            # practice — longest real pause is POST_PALETTE_GAP = 1000ms.
            while ms > 65535:
                buf.append(V3_OP_PAUSE)
                buf.append(0xFF); buf.append(0xFF)
                ms -= 65535
            buf.append(V3_OP_PAUSE)
            buf.append((ms >> 8) & 0xFF)
            buf.append(ms & 0xFF)
            continue
        if "@" in line:
            # TOK@<ms> — non-default press duration. No RLE; repeats here are
            # rare (brush/palette presses are always separated by pauses).
            tok, ms_str = line.split("@")
            ms = int(ms_str)
            if not 1 <= ms <= 255:
                raise ValueError(f"long press ms out of range: {line!r}")
            buf.append(V3_OP_LONG_PRESS | V3_IDX[tok])
            buf.append(ms)
            continue
        if "*" in line:
            tok, c = line.split("*")
            n = int(c)
        else:
            tok, n = line, 1
        idx = V3_IDX[tok]
        while n > 0:
            if n == 1:
                buf.append(V3_OP_SINGLE | idx)
                n = 0
            elif n <= 255:
                buf.append(V3_OP_REPEAT | idx)
                buf.append(n)
                n = 0
            else:
                buf.append(V3_OP_REPEAT | idx)
                buf.append(255)
                n -= 255
    return bytes(buf)


# Compact v2 format: strips the verbose `TOK 0.034s\n0.033s\n` pair down to a
# single short token, runs of the same token get RLE'd, and bare pauses are
# raw integer milliseconds. Not human-readable by design — the only consumer
# is pico/macro_runner.py. Every macro press in convert.py uses PRESS=34ms and
# a 33ms default gap, so those are implicit. Any pause other than 33ms appears
# as a separate `<ms>` line.
V2_DPAD_SHORT = {
    "DPAD_RIGHT": "R", "DPAD_LEFT": "L",
    "DPAD_UP": "U", "DPAD_DOWN": "D",
    "DPAD_UP_RIGHT": "UR", "DPAD_UP_LEFT": "UL",
    "DPAD_DOWN_RIGHT": "DR", "DPAD_DOWN_LEFT": "DL",
}
V2_STAMP_SHORT = {
    "STAMP_" + long: short.lower()
    for long, short in V2_DPAD_SHORT.items()
}
V2_BTN = {"A", "B", "X", "Y"}
V2_DEFAULT_PAUSE_MS = 33
V2_DEFAULT_PRESS_MS = 34


def to_compact(lines: list[str]) -> list[str]:
    """Rewrite v1 lines into v2 compact tokens. See V2_* maps above.

    Assumes every press line is followed by a default-pause line (convert.py
    emits them in that pair). Any extra pause lines between presses become
    bare ms lines in the output. RLE collapses runs of the same press token.
    """
    tokens: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        toks = line.split()
        if len(toks) == 2 and toks[1].endswith("s"):
            name = toks[0]
            short = (V2_DPAD_SHORT.get(name)
                     or V2_STAMP_SHORT.get(name)
                     or (name if name in V2_BTN else None))
            if short is None:
                raise ValueError(f"to_compact: unknown token {name!r}")
            press_ms = int(round(float(toks[1][:-1]) * 1000))
            if press_ms == V2_DEFAULT_PRESS_MS:
                tokens.append(short)
            else:
                tokens.append(f"{short}@{press_ms}")
            # The immediately-following line is the default post-press gap;
            # anything beyond that is an explicit extra pause.
            if i + 1 < n:
                nxt = lines[i + 1].strip().split()
                if len(nxt) == 1 and nxt[0].endswith("s"):
                    pause_ms = int(round(float(nxt[0][:-1]) * 1000))
                    if pause_ms != V2_DEFAULT_PAUSE_MS:
                        extra = pause_ms - V2_DEFAULT_PAUSE_MS
                        if extra > 0:
                            tokens.append(str(extra))
                    i += 2
                    continue
            i += 1
        elif len(toks) == 1 and line.endswith("s"):
            ms = int(round(float(line[:-1]) * 1000))
            if ms > 0:
                tokens.append(str(ms))
            i += 1
        else:
            raise ValueError(f"to_compact: unhandled line {line!r}")
    # RLE pass — only collapse runs of press tokens (letter-prefixed). Pauses
    # (digit-prefixed) stay standalone; consecutive pauses are rare and summing
    # them would hide the original structure. Long-press tokens (TOK@ms) aren't
    # RLE'd — in practice they're always separated by pauses anyway, and the
    # binary encoder doesn't support `TOK@ms*N`.
    out: list[str] = []
    j = 0
    while j < len(tokens):
        tok = tokens[j]
        if tok[0].isalpha() and "@" not in tok:
            run = 1
            while j + run < len(tokens) and tokens[j + run] == tok:
                run += 1
            out.append(f"{tok}*{run}" if run > 1 else tok)
            j += run
        else:
            out.append(tok)
            j += 1
    return out


def estimate_seconds(lines: list[str]) -> float:
    """Sum every duration token plus PRESS_OVERHEAD per press line."""
    total = 0.0
    presses = 0
    for line in lines:
        toks = line.split()
        for tok in toks:
            if tok.endswith("s"):
                try:
                    total += float(tok[:-1])
                except ValueError:
                    pass
        if len(toks) == 2 and toks[0][:1].isalpha():
            presses += 1
    return total + presses * PRESS_OVERHEAD


def generate(image_path: Path, target_size: int, suffix: str = "",
             saturation: float = 1.0,
             contrast: float = 1.0,
             sharpness: float = 1.0,
             brushes: list[str] | None = None,
             per_color: bool = False,
             use_oklab: bool = False,
             write_outputs: bool = True,
             debug: bool = False) -> dict:
    if brushes is None:
        brushes = ["1x1"]
    if "1x1" not in brushes:
        brushes = list(brushes) + ["1x1"]
    stem = image_path.stem + suffix
    out_dir = Path(__file__).parent / "out" / stem
    if write_outputs:
        out_dir.mkdir(parents=True, exist_ok=True)
    # palette.json lives in reference/ after the folder sort; fall back to the
    # image's own directory so ad-hoc runs on unsorted inputs still work.
    repo_root = Path(__file__).parent
    palette_path = repo_root / "reference" / "palette.json"
    if not palette_path.exists():
        palette_path = image_path.parent / "palette.json"
    palette = load_palette(palette_path)

    # Preserve aspect ratio: fit within target_size × target_size and pad any
    # remaining space with transparent pixels. Non-square sources (e.g. a book
    # cover sized to the 180×256 in-game book canvas) are centered rather than
    # stretched to a square; the transparent padding drops out of the mask so
    # no stamps are emitted for those cells.
    img = ImageOps.pad(
        Image.open(image_path).convert("RGBA"),
        (target_size, target_size),
        method=Image.LANCZOS,
        color=(0, 0, 0, 0),
    )
    if contrast != 1.0 or saturation != 1.0 or sharpness != 1.0:
        # Adjust on RGB only; preserve alpha so transparent regions stay
        # transparent. Order: contrast → saturation → sharpness, so edge
        # enhancement runs last on the already-punched-up colors.
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        if contrast != 1.0:
            rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
        if saturation != 1.0:
            rgb = ImageEnhance.Color(rgb).enhance(saturation)
        if sharpness != 1.0:
            rgb = ImageEnhance.Sharpness(rgb).enhance(sharpness)
        img = Image.merge("RGBA", (*rgb.split(), a))
    indices, mask = quantize(img, palette, use_oklab=use_oklab)

    quantized_rgb = palette[indices].astype(np.uint8)
    out_alpha = np.where(mask, 255, 0).astype(np.uint8)
    if write_outputs:
        Image.fromarray(np.dstack([quantized_rgb, out_alpha]), "RGBA").save(
            out_dir / f"{stem}_preview.png"
        )

    # Group pixels by color. Drawing one color at a time collapses palette ops
    # from ~(#pixels) to #unique-colors, and a dropped palette nav only corrupts
    # one color's pixels instead of cascading through the rest of the scan.
    pixels_by_color: dict[int, list[tuple[int, int]]] = {}
    for row in range(target_size):
        for col in range(target_size):
            if mask[row, col]:
                pixels_by_color.setdefault(int(indices[row, col]), []).append((col, row))

    # Color order: largest pixel-count first (duplicated in the main loop
    # but needed here to pick the right initial brush for the preamble).
    color_order = sorted(pixels_by_color,
                         key=lambda c: -len(pixels_by_color[c]))

    # Precompute cumulative "later color" pixel sets so overstamp tiling for
    # color_order[i] knows which cells it's allowed to paint over. `later[i]`
    # = union of pixels for color_order[i+1 .. ]. Computed here (before the
    # initial-brush peek) so the per-color brush picker can see it.
    later: list[set[tuple[int, int]]] = [set()] * len(color_order)
    acc: set[tuple[int, int]] = set()
    for i in range(len(color_order) - 1, -1, -1):
        later[i] = set(acc)
        acc.update(pixels_by_color[color_order[i]])

    # When per_color is set, each color gets its own brush priority list
    # picked by dry-running every candidate against that color's pixel set.
    # The global `brushes` parameter is ignored in this mode.
    per_color_brushes: list[list[str]] | None = None
    if per_color:
        per_color_brushes = _pick_per_color_brushes(
            pixels_by_color, color_order, later)
        first_brushes = per_color_brushes[0] if per_color_brushes else ["1x1"]
    else:
        first_brushes = brushes
    initial_brush = _first_used_brush(pixels_by_color, color_order, first_brushes)

    builder = MacroBuilder(initial_brush=initial_brush)
    # Assume the Pico prelude parks the cursor at canvas center (128, 128).
    # The generator emits a single long diagonal travel to reach the first
    # pixel — no manual "move to top-left before pressing start" required.
    cx, cy = 128, 128                     # canvas cursor (absolute, not relative)
    px, py = palette_xy(INITIAL_PALETTE_IDX)  # palette cursor
    stamps = 0                            # A-presses on canvas (multi-cell w/ bigger brushes)
    covered: set[tuple[int, int]] = set() # unique canvas cells hit by a stamp footprint
    switches = 0

    # Color order was computed above (needed for initial_brush peek).
    # Rationale: largest pixel-count first so big-area colors benefit most
    # from 3x3/+ brushes AND can overstamp lots of smaller colors' detail
    # pixels (later colors repaint those cells for free). Palette-walk cost
    # is ~3% of total runtime.

    # Canvas travel uses diagonal hat taps, so cost is Chebyshev distance.
    def cheby(a: tuple[int, int], b: tuple[int, int]) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def order_pixels(start: tuple[int, int], pts: list[tuple[int, int]]) -> list[tuple[int, int]]:
        # Greedy nearest-neighbor seed + 2-opt refinement. Vectorized because
        # this is the inner loop of the whole generator — every pixel of every
        # color group passes through here.
        if not pts:
            return []
        pts_arr = np.asarray(pts, dtype=np.int32)
        n = len(pts_arr)
        visited = np.zeros(n, dtype=bool)
        tour_idx = np.empty(n, dtype=np.int64)
        cx0, cy0 = start
        cur_x, cur_y = np.int32(cx0), np.int32(cy0)
        INF = np.iinfo(np.int32).max
        for step in range(n):
            d = np.maximum(np.abs(pts_arr[:, 0] - cur_x),
                           np.abs(pts_arr[:, 1] - cur_y))
            d[visited] = INF
            k = int(d.argmin())
            tour_idx[step] = k
            visited[k] = True
            cur_x, cur_y = pts_arr[k, 0], pts_arr[k, 1]
        tour_arr = pts_arr[tour_idx].copy()

        # 2-opt is O(n²) per pass and runs until convergence. Above ~500 pixels
        # color groups are dense fills where greedy-NN is already near-optimal
        # (every cell has a neighbor at distance 1) — skip the refinement.
        if n > 500:
            return [tuple(row.tolist()) for row in tour_arr]

        start_arr = np.asarray(start, dtype=np.int32)
        improved = True
        while improved:
            improved = False
            m = n
            for i in range(m - 1):
                a = tour_arr[i - 1] if i > 0 else start_arr
                b = tour_arr[i]
                c = tour_arr[i + 1:m]
                has_d = np.arange(i + 1, m) < (m - 1)
                d_src = np.minimum(np.arange(i + 2, m + 1), m - 1)
                d = tour_arr[d_src]
                ab = max(abs(int(a[0]) - int(b[0])),
                         abs(int(a[1]) - int(b[1])))
                ac = np.maximum(np.abs(c[:, 0] - a[0]),
                                np.abs(c[:, 1] - a[1]))
                cd = np.maximum(np.abs(c[:, 0] - d[:, 0]),
                                np.abs(c[:, 1] - d[:, 1]))
                bd = np.maximum(np.abs(d[:, 0] - b[0]),
                                np.abs(d[:, 1] - b[1]))
                before = ab + np.where(has_d, cd, 0)
                after = ac + np.where(has_d, bd, 0)
                improving = after < before
                if improving.any():
                    k = int(improving.argmax())
                    j = i + 1 + k
                    tour_arr[i:j + 1] = tour_arr[i:j + 1][::-1]
                    improved = True
                    break
        return [tuple(row.tolist()) for row in tour_arr]

    for idx, color in enumerate(color_order):
        target_px, target_py = palette_xy(color)
        builder.press("Y", PALETTE_PRESS, PALETTE_PAUSE)
        # Extra gap before the second Y — dropping it opens the wrong menu.
        builder.lines.append(f"{Y_GAP - PALETTE_PAUSE}s")
        builder.press("Y", PALETTE_PRESS, PALETTE_PAUSE)
        # Grid-open animation eats d-pad — wait before navigating.
        builder.lines.append(f"{POST_PALETTE_OPEN}s")
        builder.move(
            target_px - px, target_py - py,
            PALETTE_PRESS, PALETTE_PAUSE,
            diagonal=False,
        )
        builder.press("A", PALETTE_PRESS, PALETTE_PAUSE)
        builder.lines.append(f"{POST_PALETTE_GAP}s")
        px, py = target_px, target_py
        switches += 1

        # Overstampable cells = this color's own pixels ∪ later colors' pixels.
        own = pixels_by_color[color]
        overstampable = set(own) | later[idx]
        brushes_here = per_color_brushes[idx] if per_color_brushes else brushes
        by_brush = tile_color_pixels(own, overstampable, brushes_here)
        own_set = set(own)

        # Per-color, decide which brushes are worth the switch cost. See
        # module-level MIN_TILES for the threshold rationale. Under-threshold
        # groups get demoted back to 1x1 so the cells still get painted.
        demoted_cells: list[tuple[int, int]] = []
        for brush in list(by_brush):
            if brush == "1x1":
                continue
            if len(by_brush[brush]) < MIN_TILES.get(brush, 1):
                for ax, ay in by_brush[brush]:
                    for dx, dy in BRUSH_PATTERNS[brush]:
                        cell = (ax + dx, ay + dy)
                        if cell in own_set:
                            demoted_cells.append(cell)
                del by_brush[brush]
        if demoted_cells:
            by_brush.setdefault("1x1", []).extend(demoted_cells)

        # Emit each brush's sub-tour in priority order. Start each sub-tour
        # from current cursor so the 2-opt/NN ordering naturally picks the
        # nearest anchor first — minimizes inter-group jumps.
        for brush in brushes_here:
            anchors = by_brush.get(brush, [])
            if not anchors:
                continue
            builder.brush_select(brush)
            pattern = BRUSH_PATTERNS[brush]
            for col, row in order_pixels((cx, cy), anchors):
                builder.pen_to(col - cx, row - cy)
                cx, cy = col, row
                stamps += 1
                for dx, dy in pattern:
                    covered.add((col + dx, row + dy))

    macro_path = out_dir / f"{stem}_macro.mz"
    compact_lines = to_compact(builder.lines)
    macro_bytes = to_binary_v3(compact_lines)
    # 8-byte header: "MZ1" magic + version + estimated_ms (BE uint32).
    # Version byte doubles as a preamble hint:
    #   0x01 = land on 1x1 (default)
    #   0x02 = land on 3x3 (first stamp uses 3x3, skips one brush-menu cycle)
    #   0x03 = land on plus (first stamp uses plus)
    version_byte = {"3x3": 0x02, "plus": 0x03}.get(builder.initial_brush, 0x01)
    estimated_ms_for_hdr = int(round(estimate_seconds(builder.lines) * 1000))
    header = bytes([0x4D, 0x5A, 0x31, version_byte]) + estimated_ms_for_hdr.to_bytes(4, "big")
    if write_outputs:
        macro_path.write_bytes(header + macro_bytes)

    # Dry-run simulate the macro against a blank canvas and compare with the
    # quantized preview. Any mismatch means the generator or macro format is
    # wrong — better to catch here than after ten minutes of Switch time.
    simulated = simulate_macro(builder.lines, target_size, palette,
                                initial_brush=builder.initial_brush)
    # Match the simulator's convention: transparent pixels have zeroed RGB so
    # we aren't comparing the quantized fill that sits under alpha=0 regions.
    expected_rgb = np.where(mask[..., None], quantized_rgb, 0).astype(np.uint8)
    expected = np.dstack([expected_rgb, out_alpha])
    if write_outputs and debug:
        Image.fromarray(simulated, "RGBA").save(
            out_dir / f"{stem}_simulated.png"
        )
    if write_outputs:
        # 72×72 BMP thumbnail for the on-device macro grid. Crop to the
        # non-transparent bbox so small images fill the tile, composite over
        # black (BMP has no alpha), pad to square with NEAREST so the pixel
        # grid survives downscaling.
        THUMB_SIZE = 72
        sim = Image.fromarray(simulated, "RGBA")
        bbox = sim.getbbox()
        if bbox:
            sim = sim.crop(bbox)
        bg = Image.new("RGB", sim.size, (0, 0, 0))
        bg.paste(sim, mask=sim.split()[3])
        w, h = bg.size
        scale = min(THUMB_SIZE / w, THUMB_SIZE / h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        thumb = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (0, 0, 0))
        thumb.paste(
            bg.resize((new_w, new_h), Image.NEAREST),
            ((THUMB_SIZE - new_w) // 2, (THUMB_SIZE - new_h) // 2),
        )
        thumb.save(out_dir / f"{stem}_macro.bmp")
    if not np.array_equal(simulated, expected):
        diff = np.any(simulated != expected, axis=2)
        bad = int(diff.sum())
        raise RuntimeError(
            f"macro simulation disagrees with quantized preview at {bad} pixel(s)"
        )

    if per_color_brushes:
        # Report per-color choices by frequency: "3x3,1x1:15 plus,1x1:6 1x1:32"
        from collections import Counter
        counts = Counter(",".join(cs) for cs in per_color_brushes)
        reported_brushes = ["per-color"] + [f"{k}:{v}" for k, v in counts.most_common()]
    else:
        reported_brushes = brushes
    info = {
        "source": image_path.name,
        "size": target_size,
        "cells_covered": len(covered),
        "canvas_cells": target_size * target_size,
        "color_switches": switches,
        "brush_switches": builder.brush_switches,
        "brushes": reported_brushes,
        "quantizer": "oklab" if use_oklab else "rgb",
        "stamps": stamps,
        "button_presses": builder.presses,
        "estimated_seconds": estimate_seconds(builder.lines),
        "press_duration": PRESS,
        "pause_duration": PAUSE,
        "initial_palette_idx": INITIAL_PALETTE_IDX,
        "initial_brush": builder.initial_brush,
    }
    if write_outputs and debug:
        (out_dir / f"{stem}_macro_info.json").write_text(
            json.dumps(info, indent=2)
        )
    info["macro_path"] = str(macro_path)
    return info


CANVAS_SIZE = 256


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("image", type=Path)
    p.add_argument("--suffix", type=str, default="",
                   help="appended to output basename, e.g. '_vivid'")
    p.add_argument("--saturation", type=float, default=1.0,
                   help="saturation multiplier applied to source before quantization")
    p.add_argument("--contrast", type=float, default=1.0,
                   help="contrast multiplier applied to source before quantization")
    p.add_argument("--sharpness", type=float, default=1.0,
                   help="sharpness multiplier applied to source before quantization")
    p.add_argument("--brushes", type=str, default="auto",
                   help="comma-separated brush priority list, largest-first "
                        "(available: 3x3, plus, 1x1). 'auto' dry-runs every "
                        "combination and picks the fastest.")
    p.add_argument("--quant", choices=["rgb", "oklab"], default="rgb",
                   help="color-distance metric for palette snap. 'oklab' is "
                        "more vivid but slower at runtime.")
    p.add_argument("--debug", action="store_true",
                   help="also write _simulated.png and _macro_info.json "
                        "alongside the macro + thumbnail.")
    args = p.parse_args()

    print(f"Converting {args.image}...", flush=True)

    use_oklab = args.quant == "oklab"

    if args.brushes == "auto":
        # Dry-run every brush combo and pick the fastest by end-to-end estimate.
        # per-color picks the cheapest brush set INDEPENDENTLY for each color
        # group; wins on images with mixed-topology colors (sparse dots + dense
        # fills). Keeping the single-set candidates in the pool bounds the
        # worst-case so per-color can't regress below the best global choice.
        from concurrent.futures import ProcessPoolExecutor
        candidates = [
            ("1x1",          ["1x1"],                False),
            ("3x3,1x1",      ["3x3", "1x1"],         False),
            ("plus,1x1",     ["plus", "1x1"],        False),
            ("3x3,plus,1x1", ["3x3", "plus", "1x1"], False),
            ("per-color",    ["3x3", "plus", "1x1"], True),
        ]
        with ProcessPoolExecutor(max_workers=len(candidates)) as pool:
            futures = [
                pool.submit(generate, args.image, CANVAS_SIZE,
                            suffix=args.suffix, saturation=args.saturation,
                            contrast=args.contrast, sharpness=args.sharpness,
                            brushes=cfg, per_color=pc, use_oklab=use_oklab,
                            write_outputs=False)
                for _, cfg, pc in candidates
            ]
            results = [(f.result()["estimated_seconds"], label, cfg, pc)
                       for f, (label, cfg, pc) in zip(futures, candidates)]
        results.sort()
        print("Auto-brush evaluation:")
        winner_label = results[0][1]
        for secs, label, cfg, pc in results:
            mark = "* " if label == winner_label else "  "
            print(f"  {mark}{label:20s}  {secs:.0f}s ({secs/60:.1f} min)")
        _, _, brushes, per_color_flag = results[0]
    else:
        per_color_flag = False
        brushes = [b.strip() for b in args.brushes.split(",") if b.strip()]
        unknown = [b for b in brushes if b not in BRUSH_PATTERNS]
        if unknown:
            raise SystemExit(f"unknown brush name(s): {unknown}; "
                             f"available: {list(BRUSH_PATTERNS)}")

    info = generate(args.image, CANVAS_SIZE, suffix=args.suffix,
                    saturation=args.saturation, contrast=args.contrast,
                    sharpness=args.sharpness, brushes=brushes,
                    per_color=per_color_flag,
                    use_oklab=use_oklab, debug=args.debug)
    print(f"Source:           {info['source']} -> {info['size']}x{info['size']}")
    print(f"Coverage:         {info['cells_covered']}/{info['canvas_cells']} "
          f"({100*info['cells_covered']/info['canvas_cells']:.1f}%)")
    print(f"Stamps:           {info['stamps']}")
    print(f"Color switches:   {info['color_switches']}")
    print(f"Brush switches:   {info['brush_switches']}  ({','.join(info['brushes'])})")
    print(f"Button presses:   {info['button_presses']}")
    secs = info["estimated_seconds"]
    print(f"Estimated runtime: {secs:.0f}s ({secs/60:.1f} min)")
    print(f"Macro:            {info['macro_path']}")


if __name__ == "__main__":
    main()
