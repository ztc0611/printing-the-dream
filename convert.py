#!/usr/bin/env python3
"""
convert.py — turn a PNG into a macro that draws it in the Tomodachi Life item
creator. Runs on the Pico CircuitPython build and the legacy Pi Zero NXBT
runner. Color-grouped TSP-ordered path, multi-brush, skips A-press on
transparent pixels.

Usage:
    python3 convert.py <image.png> [--size N]

Outputs (default):
    <name>.mz                binary macro for the Pico (BMP thumbnail
                              embedded in the MZ2 header)
    <name>_preview.png       full-res palette-snap preview (not for the Pico)

Additional outputs with --debug:
    <name>_simulated.png     dry-run trace of what the macro will draw
    <name>.json              generation stats + starting-state assumptions

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
from collections import deque
from pathlib import Path
import argparse
import json

import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from scipy.ndimage import label as scipy_label

# Per-press tap timing. 34/33ms is the reliable floor.
PRESS = 0.034
PAUSE = 0.033
# Tight transitions around the canvas A-stamp.
PRE_A_PAUSE = 0.033
A_PAUSE = 0.033
# STAMP combines the last move step with the A press into a single HID report
# (A + d-pad held in the same frame). Game applies both atomically, so the
# stamp lands on the destination cell — saves one press cycle per pixel.
STAMP_PRESS = 0.034
STAMP_PAUSE = 0.033
# Palette nav cadence. The bottleneck is the grid-open animation, not the
# tap-to-tap rate (see POST_PALETTE_OPEN below).
PALETTE_PRESS = 0.067
PALETTE_PAUSE = 0.133
# Brush menu is slower than palette — a dropped A leaves the real brush
# wrong and 3x3 stamps degrade to 1x1.
BRUSH_PRESS = 0.1
BRUSH_PAUSE = 0.2
# Gap between the two Y presses (sidebar → full palette transition).
Y_GAP = 0.15
# Palette grid open animation eats d-pad inputs.
POST_PALETTE_OPEN = 0.3
# Palette close animation eats canvas d-pad inputs.
POST_PALETTE_GAP = 1.0
# Per-press wall-clock overhead not captured by duration tokens, applied
# in estimate_seconds so the .mz header + countdown match reality.
PRESS_OVERHEAD = 0.0042
# Bucket fill costs. In-game bucket animation is functionally instant; only the
# tool-switch menu has measurable latency. PER_FILL_S is the typical
# cursor-nav-to-interior + A press. BRUSH_PER_CELL_S is the v2 set-cover
# average for the break-even check.
BUCKET_SWITCH_S = 4.4
PER_FILL_S = 1.0
BRUSH_PER_CELL_S = 0.0055
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
#  row 1:  1x1    3x3     7x7
BRUSH_POSITIONS: dict[str, tuple[int, int]] = {
    "null": (2, 0),
    "plus": (1, 0),
    "1x1":  (0, 1),
    "3x3":  (1, 1),
    "7x7":  (2, 1),
}
BRUSH_PATTERNS: dict[str, list[tuple[int, int]]] = {
    "1x1":  [(0, 0)],
    "3x3":  [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)],
    "plus": [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)],
    "7x7":  [(dx, dy) for dy in range(-3, 4) for dx in range(-3, 4)],
    "null": [],  # Sentinel for the fresh-file state; never emits stamps.
}
INITIAL_BRUSH = "1x1"  # post-preamble state; macro_runner selects 1x1 from null
# Minimum tiles a non-1x1 brush must place to cover its switch cost.
MIN_TILES = {"3x3": 3, "plus": 5, "7x7": 1}


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
    ("7x7", "3x3", "1x1"),
    ("7x7", "3x3", "plus", "1x1"),
    ("1x1",),
)


def _eval_brush_set(start, own, overstampable, brush_set, prior_brush,
                    placement_cache=None):
    """Estimate wall-clock cost of painting `own` with a given brush priority.
    Returns (cost_seconds, end_cursor, end_brush). Uses _set_cover_color +
    MIN_TILES demotion and a greedy-NN tour for travel — no 2-opt, since this
    runs per-candidate-per-color and has to be fast."""
    by_brush = _set_cover_color(own, overstampable, list(brush_set),
                                 placement_cache=placement_cache)
    for b in list(by_brush):
        if b == "1x1":
            continue
        if len(by_brush[b]) < MIN_TILES.get(b, 1):
            del by_brush[b]
    travel_s = PRESS + PAUSE + PRESS_OVERHEAD
    # STAMP merges the final move-cell with the A-press, so a chebyshev-N hop
    # costs N*travel_s. Adding a separate per-anchor stamp term double-counts
    # and biases the picker toward stride-5 brushes over stride-1.
    brush_switch_s = 4.0  # matches MIN_TILES rationale at module top
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
        # Vectorized greedy-NN tour length (matches order_pixels' NN seed).
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
    return total, cur, cur_brush


def _pick_per_color_brushes(pixels_by_color, color_order, later,
                             placement_caches=None):
    """For each color, evaluate every candidate brush set and keep the
    cheapest. Returns parallel list to color_order — each entry is the brush
    priority list to use for that color's tiling.

    `placement_caches`, if provided, is populated with one cache per color so
    the final emit pass can reuse the picker's enumerated placements instead
    of re-enumerating from scratch."""
    cx, cy = 128, 128
    cur_brush = INITIAL_BRUSH
    picks: list[list[str]] = []
    for idx, color in enumerate(color_order):
        own = pixels_by_color[color]
        overstampable = set(own) | later[idx]
        cache: dict = {}
        if placement_caches is not None:
            placement_caches[color] = cache
        best = None
        for cs in _PER_COLOR_CANDIDATES:
            cost, end_cur, end_brush = _eval_brush_set(
                (cx, cy), own, overstampable, cs, cur_brush,
                placement_cache=cache)
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
    by_brush = _set_cover_color(own, overstampable, brushes)
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
        # BRUSH_PAUSE between Xs is enough; no extra inter-X gap.
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


def _enumerate_valid_placements(own_set: frozenset, overstampable: frozenset,
                                 brush: str):
    """Return (own_list, placements) for one (color, brush) pair.

    `placements` is a list of (ax, ay, own_cell_indices) where every footprint
    cell is in `overstampable` and at least one is own-color. Anchors do NOT
    need to sit on an own-pixel — any anchor whose footprint touches own is
    fair game, which lets 7x7/3x3 placements straddle the boundary between own
    and later-color cells (the densest packings live there).
    """
    pat = BRUSH_PATTERNS[brush]
    radius = max(max(abs(dx), abs(dy)) for dx, dy in pat)

    own_list = sorted(own_set)
    own_idx = {p: i for i, p in enumerate(own_list)}

    cand = set()
    for ox, oy in own_list:
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                cand.add((ox + dx, oy + dy))

    placements = []
    for ax, ay in cand:
        cells = [(ax + dx, ay + dy) for dx, dy in pat]
        if not all(c in overstampable for c in cells):
            continue
        own_cells = [own_idx[c] for c in cells if c in own_idx]
        if not own_cells:
            continue
        placements.append((ax, ay, np.asarray(own_cells, dtype=np.int32)))
    return own_list, placements


def _greedy_set_cover_one_brush(own_list, placements, remaining_mask):
    """Greedy max-coverage set cover. Picks the placement that covers the most
    still-uncovered own cells, removes those cells, repeats until no placement
    adds ≥1 new own cell. Mutates `remaining_mask` in place."""
    if not placements:
        return []
    n = len(placements)
    fp_sz = max(len(own_cells) for (_, _, own_cells) in placements)
    foot = np.full((n, fp_sz), -1, dtype=np.int32)
    for i, (_, _, own_cells) in enumerate(placements):
        foot[i, :len(own_cells)] = own_cells
    anchors = []
    active = np.ones(n, dtype=bool)
    while True:
        sentinel = len(remaining_mask)
        idx = np.where(foot < 0, sentinel, foot)
        ext = np.concatenate([remaining_mask, np.zeros(1, dtype=bool)])
        gains = ext[idx].sum(axis=1)
        gains[~active] = 0
        best = int(gains.argmax())
        if int(gains[best]) < 1:
            break
        ax, ay, own_cells = placements[best]
        anchors.append((ax, ay))
        remaining_mask[own_cells] = False
        active[best] = False
    return anchors


def _set_cover_color(own_pixels, overstampable, brushes_priority,
                     placement_cache=None):
    """Tile one color via greedy set-cover, brush by brush in priority order.

    `placement_cache`, if provided, maps brush_name -> (own_list, placements)
    pre-enumerated by the caller. Lets the per-color picker enumerate each
    brush once and reuse across 6 candidate brush sets instead of redoing
    the work per candidate.

    Returns {brush: [anchor, ...]} including a final "1x1" group for any cells
    not covered by larger brushes.
    """
    own_set = frozenset(own_pixels)
    overstampable_set = frozenset(overstampable)
    out: dict[str, list[tuple[int, int]]] = {}

    own_list = None
    remaining_mask = None

    for brush in brushes_priority:
        if brush == "1x1":
            continue
        if placement_cache is not None and brush in placement_cache:
            own_list_b, placements = placement_cache[brush]
        else:
            own_list_b, placements = _enumerate_valid_placements(
                own_set, overstampable_set, brush)
            if placement_cache is not None:
                placement_cache[brush] = (own_list_b, placements)
        if own_list is None:
            own_list = own_list_b
            remaining_mask = np.ones(len(own_list), dtype=bool)
        anchors = _greedy_set_cover_one_brush(own_list, placements,
                                              remaining_mask)
        if anchors:
            out[brush] = anchors
    if own_list is not None:
        leftover = [own_list[i] for i in range(len(own_list))
                    if remaining_mask[i]]
    else:
        leftover = list(own_pixels)
    if leftover:
        out["1x1"] = leftover
    return out


def simulate_macro(
    macro_lines: list[str], size: int, palette: np.ndarray,
    initial_brush: str = INITIAL_BRUSH,
) -> np.ndarray:
    """Replay the macro against a fresh item-file state and return the RGBA
    image it would leave on the canvas. Modes:
      canvas         — DPAD moves cx/cy; A paints with the current tool
                        (brush stamps current_brush, bucket flood-fills the
                        connected same-state region).
      palette_side   — Y opens the 10-slot sidebar first
      palette_grid   — second Y opens the full 12×7 grid; DPAD moves px/py,
                       A commits the new color and returns to canvas.
      brush_side     — X opens the brush sidebar — DPAD shifts a relative
                       cursor (LEFT goes to bucket, RIGHT goes to brush),
                       A commits the tool and returns to canvas.
      brush_grid     — second X opens the full brush grid; DPAD moves bx/by,
                       A commits the brush and returns to canvas.
    """
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    cx, cy = 128, 128
    px, py = palette_xy(INITIAL_PALETTE_IDX)
    color = INITIAL_PALETTE_IDX
    brush = initial_brush
    bx, by = BRUSH_POSITIONS[brush]
    tool = "brush"
    mode = "canvas"
    # Relative sidebar cursor offset from the current tool: -1 = bucket from
    # brush, +1 = brush from bucket. Reset to 0 when sidebar opens.
    sidebar_offset = 0

    def stamp_at(ax: int, ay: int) -> None:
        for dx, dy in BRUSH_PATTERNS[brush]:
            nx, ny = ax + dx, ay + dy
            if 0 <= nx < size and 0 <= ny < size:
                canvas[ny, nx, :3] = palette[color].astype(np.uint8)
                canvas[ny, nx, 3] = 255

    def bucket_fill(ax: int, ay: int) -> None:
        if not (0 <= ax < size and 0 <= ay < size):
            return
        target_alpha = int(canvas[ay, ax, 3])
        target_rgb = tuple(int(v) for v in canvas[ay, ax, :3])
        new_rgb = tuple(int(v) for v in palette[color].astype(np.uint8))
        if target_alpha == 255 and target_rgb == new_rgb:
            return
        visited = np.zeros((size, size), dtype=bool)
        q = deque([(ax, ay)])
        visited[ay, ax] = True
        while q:
            x, y = q.popleft()
            canvas[y, x, 0] = new_rgb[0]
            canvas[y, x, 1] = new_rgb[1]
            canvas[y, x, 2] = new_rgb[2]
            canvas[y, x, 3] = 255
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < size and 0 <= ny < size):
                    continue
                if visited[ny, nx]:
                    continue
                if int(canvas[ny, nx, 3]) != target_alpha:
                    continue
                if target_alpha == 255 and tuple(
                        int(v) for v in canvas[ny, nx, :3]) != target_rgb:
                    continue
                visited[ny, nx] = True
                q.append((nx, ny))

    for line in macro_lines:
        toks = line.strip().split()
        if len(toks) < 2 or not toks[-1].endswith("s"):
            continue
        btn = toks[0]
        if btn == "Y":
            mode = "palette_side" if mode == "canvas" else "palette_grid"
        elif btn == "X":
            if mode == "canvas":
                mode = "brush_side"
                sidebar_offset = 0
            else:
                mode = "brush_grid"
        elif btn == "A":
            if mode == "canvas":
                if tool == "bucket":
                    bucket_fill(cx, cy)
                else:
                    stamp_at(cx, cy)
            elif mode == "palette_grid":
                color = py * PALETTE_COLS + px
                mode = "canvas"
            elif mode == "brush_grid":
                for name, pos in BRUSH_POSITIONS.items():
                    if pos == (bx, by):
                        brush = name
                        tool = "brush"
                        break
                mode = "canvas"
            elif mode == "brush_side":
                # Sidebar tool select: LEFT (offset -1) from brush goes to
                # bucket; RIGHT (offset +1) from bucket goes back to brush.
                if tool == "brush" and sidebar_offset == -1:
                    tool = "bucket"
                elif tool == "bucket" and sidebar_offset == 1:
                    tool = "brush"
                mode = "canvas"
        elif btn == "B":
            mode = "canvas"
        elif btn.startswith("DPAD_"):
            dirs = btn.split("_", 1)[1]
            dx = (1 if "RIGHT" in dirs else 0) - (1 if "LEFT" in dirs else 0)
            dy = (1 if "DOWN" in dirs else 0) - (1 if "UP" in dirs else 0)
            if mode == "canvas":
                cx += dx; cy += dy
            elif mode == "palette_grid":
                if not (dx and dy):
                    px += dx; py += dy
            elif mode == "brush_grid":
                if not (dx and dy):
                    bx += dx; by += dy
            elif mode == "brush_side":
                sidebar_offset += dx
        elif btn.startswith("STAMP_DPAD_"):
            dirs = btn.split("_", 2)[2]
            dx = (1 if "RIGHT" in dirs else 0) - (1 if "LEFT" in dirs else 0)
            dy = (1 if "DOWN" in dirs else 0) - (1 if "UP" in dirs else 0)
            if mode == "canvas":
                cx += dx; cy += dy
                if tool == "bucket":
                    bucket_fill(cx, cy)
                else:
                    stamp_at(cx, cy)
    return canvas


def _find_bucket_color(pixels_by_color, mask):
    """Pick the single color whose deferred-bucket-fill would save the most
    runtime. Returns (color, components, savings_s) or None.

    A color is eligible iff every one of its connected components (4-conn,
    computed on own_mask | truly_transparent) contains no truly-transparent
    cells. Components that touch the transparent padding are skipped — a
    bucket-fill there would paint the padding and corrupt the output.

    Components are stored as a (k, 2) int32 array of all own cells so the
    emit pass can enter each component via the cell closest to the current
    cursor instead of always the top-left."""
    truly_transparent = ~mask
    best = None
    for c, own_cells in pixels_by_color.items():
        own_mask = np.zeros_like(mask)
        for x, y in own_cells:
            own_mask[y, x] = True
        unpainted = own_mask | truly_transparent
        labels, n_comp = scipy_label(
            unpainted,
            structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]),
        )
        components = []
        eligible = True
        for comp_id in range(1, n_comp + 1):
            comp_mask = (labels == comp_id)
            if (comp_mask & truly_transparent).any():
                if (comp_mask & own_mask).any():
                    eligible = False
                    break
                continue
            ys, xs = np.where(comp_mask)
            cells = np.stack([xs.astype(np.int32), ys.astype(np.int32)],
                              axis=1)
            components.append((cells, int(comp_mask.sum())))
        if not eligible or not components:
            continue
        total_cells = sum(n for _, n in components)
        bucket_cost = BUCKET_SWITCH_S + len(components) * PER_FILL_S
        brush_cost = total_cells * BRUSH_PER_CELL_S
        savings = brush_cost - bucket_cost
        if savings > 0 and (best is None or savings > best[2]):
            best = (c, components, savings)
    return best


def _emit_tool_switch(builder, target_tool: str, current_tool: str) -> None:
    """Switch between brush and bucket via the X sidebar.
    From brush: X-LEFT-A. From bucket: X-RIGHT-A. Cost ~2.2s per direction."""
    if target_tool == current_tool:
        return
    builder.press("X", BRUSH_PRESS, BRUSH_PAUSE)
    builder.lines.append(f"{POST_PALETTE_OPEN}s")
    if target_tool == "bucket" and current_tool == "brush":
        builder.press("DPAD_LEFT", BRUSH_PRESS, BRUSH_PAUSE)
    elif target_tool == "brush" and current_tool == "bucket":
        builder.press("DPAD_RIGHT", BRUSH_PRESS, BRUSH_PAUSE)
    else:
        raise ValueError(
            f"unsupported tool transition: {current_tool} -> {target_tool}")
    builder.press("A", BRUSH_PRESS, BRUSH_PAUSE)
    builder.lines.append(f"{POST_PALETTE_GAP}s")


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
# Long-press: [0x40 | idx] [ms]. Encodes durations up to 255ms — used by
# brush/palette menu presses that need to outlast the 34ms STAMP cadence.
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
    """Sum every duration token plus a per-press overhead term. Duration tokens
    capture the planned on-wire time; PRESS_OVERHEAD covers USB-poll wait and
    firmware dispatch that aren't in the macro text. Without it, a 30k-press
    print undercounts by ~2 min."""
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
             saturation: float = 1.0, contrast: float = 1.0,
             sharpness: float = 1.0,
             a_pause: float | None = None,
             pre_a_pause: float | None = None,
             brushes: list[str] | None = None,
             per_color: bool = True,
             use_oklab: bool = False,
             use_bucket: bool = True,
             write_outputs: bool = True,
             debug: bool = False) -> dict:
    global A_PAUSE, PRE_A_PAUSE
    if a_pause is not None:
        A_PAUSE = a_pause
    if pre_a_pause is not None:
        PRE_A_PAUSE = pre_a_pause
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
    # remaining space with transparent pixels. Non-square sources (book covers
    # at 180×256, DVD/TV at 256×131, games at 256×144) get centered instead of
    # stretched to square; the transparent padding drops out of the mask so no
    # stamps are emitted for those cells.
    img = ImageOps.pad(
        Image.open(image_path).convert("RGBA"),
        (target_size, target_size),
        method=Image.LANCZOS,
        color=(0, 0, 0, 0),
    )
    if contrast != 1.0 or saturation != 1.0 or sharpness != 1.0:
        # Adjust on RGB only; preserve alpha so transparent regions stay
        # transparent. Order: contrast → saturation → sharpness, so edge
        # enhancement runs last on the already-punched-up colors. Muted
        # sources snap to vivid palette entries; sharpness fights the
        # LANCZOS-downscale blur before the palette snap.
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

    # Bucket scheduling: pick a single color worth deferring to last and
    # painting via paint-bucket fills. Done BEFORE `later[]` is built so we
    # can exclude bucket-color cells from earlier colors' overstamp set —
    # otherwise earlier 7x7 stamps would paint into bucket-color cells with
    # the wrong color, breaking the transparent-fill assumption.
    bucket_pick = _find_bucket_color(pixels_by_color, mask) if use_bucket else None
    bucket_color = bucket_pick[0] if bucket_pick else None
    bucket_components = bucket_pick[1] if bucket_pick else None
    if bucket_color is not None:
        color_order = [c for c in color_order if c != bucket_color] + [bucket_color]

    # Precompute cumulative "later color" pixel sets so overstamp tiling for
    # color_order[i] knows which cells it's allowed to paint over. `later[i]`
    # = union of pixels for color_order[i+1 .. ], EXCLUDING bucket-color cells
    # (those must remain transparent until the bucket-fill pass).
    later: list[set[tuple[int, int]]] = [set()] * len(color_order)
    acc: set[tuple[int, int]] = set()
    for i in range(len(color_order) - 1, -1, -1):
        later[i] = set(acc)
        if color_order[i] != bucket_color:
            acc.update(pixels_by_color[color_order[i]])

    # When per_color is set, each color gets its own brush priority list
    # picked by dry-running every candidate against that color's pixel set.
    # Skip the bucket color — it doesn't use a brush.
    per_color_brushes: list[list[str]] | None = None
    placement_caches: dict = {}
    if per_color:
        non_bucket_pbc = {c: v for c, v in pixels_by_color.items()
                          if c != bucket_color}
        non_bucket_order = [c for c in color_order if c != bucket_color]
        non_bucket_later = [later[color_order.index(c)] for c in non_bucket_order]
        nb_picks = _pick_per_color_brushes(
            non_bucket_pbc, non_bucket_order, non_bucket_later,
            placement_caches=placement_caches)
        nb_iter = iter(nb_picks)
        per_color_brushes = [
            None if c == bucket_color else next(nb_iter)
            for c in color_order
        ]
        first_brushes = (per_color_brushes[0]
                          if per_color_brushes and per_color_brushes[0]
                          else ["1x1"])
    else:
        first_brushes = brushes

    if color_order and color_order[0] != bucket_color:
        initial_brush = _first_used_brush(pixels_by_color, color_order, first_brushes)
    else:
        initial_brush = INITIAL_BRUSH

    builder = MacroBuilder(initial_brush=initial_brush)
    # v2 .mz files embed the brush-selection sequence at the head of the
    # opcode stream so the runner is fully data-driven. Brush cursor parks
    # on "null" (2, 0) on a fresh item file — emit navigation from there
    # to the macro's actual initial brush. brush_select bumps the
    # switch-count stat; reset so the embedded preamble doesn't pollute
    # the report.
    builder.current_brush = "null"
    builder.brush_select(initial_brush)
    builder.brush_switches = 0
    cx, cy = 128, 128                     # canvas cursor (absolute, not relative)
    px, py = palette_xy(INITIAL_PALETTE_IDX)  # palette cursor
    stamps = 0                            # A-presses on canvas (multi-cell w/ bigger brushes)
    covered: set[tuple[int, int]] = set() # unique canvas cells hit by a stamp footprint
    switches = 0
    bucket_fills = 0
    current_tool = "brush"

    # Color order was computed above (needed for initial_brush peek).
    # Rationale: largest pixel-count first so big-area colors benefit most
    # from 3x3/+ brushes AND can overstamp lots of smaller colors' detail
    # pixels (later colors repaint those cells for free). Palette-walk cost
    # is ~3% of total runtime; no whites-last override.

    # Canvas travel uses diagonal hat taps, so cost is Chebyshev distance.
    def cheby(a: tuple[int, int], b: tuple[int, int]) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def order_pixels(start: tuple[int, int], pts: list[tuple[int, int]]) -> list[tuple[int, int]]:
        # Greedy nearest-neighbor seed + 2-opt refinement, vectorized.
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

        # 2-opt is O(n²); skip on dense groups where NN is already optimal.
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
                # Vectorize the inner j-loop. cheby deltas for all j in [i+1, m).
                c = tour_arr[i + 1:m]                       # (m-i-1, 2)
                has_d = np.arange(i + 1, m) < (m - 1)
                # d[k] = tour_arr[i+1+k+1] when has_d[k] else dummy (not used)
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
                    k = int(improving.argmax())  # first-improvement match
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

        if color == bucket_color:
            # Bucket-fill pass: switch to bucket, fill each component (entering
            # via its closest cell to the current cursor), switch back.
            _emit_tool_switch(builder, "bucket", current_tool)
            current_tool = "bucket"
            remaining = list(range(len(bucket_components)))
            while remaining:
                best_idx = None
                best_cell = None
                best_dist = None
                for ri in remaining:
                    cells, _ = bucket_components[ri]
                    d = np.maximum(np.abs(cells[:, 0] - cx),
                                   np.abs(cells[:, 1] - cy))
                    k = int(d.argmin())
                    dist = int(d[k])
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best_idx = ri
                        best_cell = (int(cells[k, 0]), int(cells[k, 1]))
                ax, ay = best_cell
                builder.move(ax - cx, ay - cy)
                cx, cy = ax, ay
                builder.press("A", PRESS, PAUSE)
                bucket_fills += 1
                remaining.remove(best_idx)
            for own_pt in pixels_by_color[color]:
                covered.add(own_pt)
            _emit_tool_switch(builder, "brush", current_tool)
            current_tool = "brush"
            continue

        # Overstampable cells = this color's own pixels ∪ later colors' pixels.
        own = pixels_by_color[color]
        overstampable = set(own) | later[idx]
        brushes_here = per_color_brushes[idx] if per_color_brushes else brushes
        if "1x1" not in brushes_here:
            brushes_here = list(brushes_here) + ["1x1"]
        by_brush = _set_cover_color(own, overstampable, brushes_here,
                                     placement_cache=placement_caches.get(color))
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
            existing = set(by_brush.get("1x1", []))
            existing.update(demoted_cells)
            by_brush["1x1"] = list(existing)

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

    macro_path = out_dir / f"{stem}.mz"
    compact_lines = to_compact(builder.lines)
    macro_bytes = to_binary_v3(compact_lines)

    # Dry-run simulate the macro against a blank canvas and compare with the
    # quantized preview. Any mismatch means the generator or macro format is
    # wrong — better to catch here than after ten minutes of Switch time.
    # Start the simulator with brush="null" (2, 0) to match the real
    # fresh-file state, so the embedded brush preamble navigates from
    # the same origin the macro itself assumes.
    simulated = simulate_macro(builder.lines, target_size, palette,
                                initial_brush="null")
    # Match the simulator's convention: transparent pixels have zeroed RGB so
    # we aren't comparing the quantized fill that sits under alpha=0 regions.
    expected_rgb = np.where(mask[..., None], quantized_rgb, 0).astype(np.uint8)
    expected = np.dstack([expected_rgb, out_alpha])
    if write_outputs and debug:
        Image.fromarray(simulated, "RGBA").save(
            out_dir / f"{stem}_simulated.png"
        )

    # 72×72 BMP thumbnail for the on-device macro grid. Crop to the
    # non-transparent bbox so small images fill the tile, composite over
    # black (BMP has no alpha), pad to square with NEAREST so the pixel
    # grid survives downscaling. Generated unconditionally because v2 .mz
    # files embed it in the header.
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
    import io
    bmp_buf = io.BytesIO()
    thumb.save(bmp_buf, format="BMP")
    bmp_bytes = bmp_buf.getvalue()

    # v3 .mz layout — BMP at offset 0 so CP's displayio.OnDiskBitmap works
    # directly (it reads the BMP header from position 0 and uses absolute
    # f_lseek for pixel data; can't be coaxed into an offset). Macro
    # trailer comes after the BMP:
    #   offset 0          BMP file (bmp_size bytes, ends at BMP's bfSize)
    #   offset bmp_size   "MZ3" magic (3 bytes)
    #   offset bmp_size+3 version byte (low nibble = preamble brush,
    #                                   high bit  = paint-bucket scheduling)
    #   offset bmp_size+4 estimated_ms  BE uint32
    #   offset bmp_size+8 macro_size    BE uint32 (length of opcode stream)
    #   offset bmp_size+12 ...          opcode stream
    # Runners detect format from first 2 bytes ("BM" = v3, "MZ" = v1/v2).
    version_byte = {"3x3": 0x02, "plus": 0x03, "7x7": 0x04}.get(builder.initial_brush, 0x01)
    if bucket_color is not None:
        version_byte |= 0x80
    estimated_ms_for_hdr = int(round(estimate_seconds(builder.lines) * 1000))
    mz3_trailer = (bytes([0x4D, 0x5A, 0x33, version_byte])
                   + estimated_ms_for_hdr.to_bytes(4, "big")
                   + len(macro_bytes).to_bytes(4, "big"))
    if write_outputs:
        macro_path.write_bytes(bmp_bytes + mz3_trailer + macro_bytes)
    if not np.array_equal(simulated, expected):
        diff = np.any(simulated != expected, axis=2)
        bad = int(diff.sum())
        raise RuntimeError(
            f"macro simulation disagrees with quantized preview at {bad} pixel(s)"
        )

    if per_color_brushes:
        from collections import Counter
        counts = Counter(",".join(cs) for cs in per_color_brushes if cs)
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
        "bucket_color": bucket_color,
        "bucket_fills": bucket_fills,
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
        (out_dir / f"{stem}.json").write_text(
            json.dumps(info, indent=2)
        )
    info["macro_path"] = str(macro_path)
    return info


def main() -> None:
    global PRESS, PAUSE, PALETTE_PRESS, PALETTE_PAUSE
    p = argparse.ArgumentParser()
    p.add_argument("image", type=Path)
    p.add_argument("--size", type=int, default=256)
    p.add_argument("--press", type=float, help="override canvas press duration (s)")
    p.add_argument("--pause", type=float, help="override canvas pause duration (s)")
    p.add_argument("--suffix", type=str, default="",
                   help="appended to output basename, e.g. '_slow'")
    p.add_argument("--saturation", type=float, default=1.0)
    p.add_argument("--contrast", type=float, default=1.0)
    p.add_argument("--sharpness", type=float, default=1.0)
    p.add_argument("--a-pause", type=float, default=None)
    p.add_argument("--pre-a-pause", type=float, default=None)
    p.add_argument("--brushes", type=str, default="7x7,3x3,plus,1x1",
                   help="comma-separated brush priority list, largest-first "
                        "(available: 7x7, 3x3, plus, 1x1). Per-color picks "
                        "the cheapest subset per color group automatically.")
    p.add_argument("--no-bucket", action="store_true",
                   help="disable paint-bucket scheduling")
    p.add_argument("--quant", choices=["rgb", "oklab", "auto"], default="auto",
                   help="palette-snap metric. 'auto' tries both and picks "
                        "oklab if its runtime overhead is within EITHER the "
                        "percent or absolute-minute threshold.")
    p.add_argument("--oklab-threshold-pct", type=float, default=10.0,
                   help="max percent runtime overhead for oklab in auto mode.")
    p.add_argument("--oklab-threshold-min", type=float, default=3.0,
                   help="max absolute-minute runtime overhead for oklab in "
                        "auto mode.")
    p.add_argument("--debug", action="store_true",
                   help="also write _simulated.png and a stats JSON.")
    args = p.parse_args()

    print(f"Converting {args.image}...", flush=True)

    if args.press is not None:
        PRESS = args.press
    if args.pause is not None:
        PAUSE = args.pause

    brushes = [b.strip() for b in args.brushes.split(",") if b.strip()]
    unknown = [b for b in brushes if b not in BRUSH_PATTERNS]
    if unknown:
        raise SystemExit(f"unknown brush name(s): {unknown}; "
                         f"available: {list(BRUSH_PATTERNS)}")
    common = dict(
        target_size=args.size, suffix=args.suffix,
        saturation=args.saturation, contrast=args.contrast,
        sharpness=args.sharpness, a_pause=args.a_pause,
        pre_a_pause=args.pre_a_pause, brushes=brushes,
        per_color=True, use_bucket=not args.no_bucket, debug=args.debug,
    )

    if args.quant == "auto":
        # Dry-run both quants in parallel (no writes), pick oklab when its
        # runtime overhead clears the percent OR the absolute-minute floor.
        # OR-logic protects short prints from being denied oklab over a
        # large proportional cost that's tiny in absolute terms.
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=2) as pool:
            f_rgb = pool.submit(generate, args.image,
                                 use_oklab=False, write_outputs=False, **common)
            f_oklab = pool.submit(generate, args.image,
                                   use_oklab=True, write_outputs=False, **common)
            rgb_secs = f_rgb.result()["estimated_seconds"]
            oklab_secs = f_oklab.result()["estimated_seconds"]
        delta_s = oklab_secs - rgb_secs
        pct = (delta_s / rgb_secs * 100.0) if rgb_secs > 0 else float("inf")
        within_pct = pct <= args.oklab_threshold_pct
        within_abs = delta_s <= args.oklab_threshold_min * 60.0
        use_oklab = within_pct or within_abs
        chosen = "oklab" if use_oklab else "rgb"
        print(f"Quant evaluation:")
        print(f"  rgb     {rgb_secs:7.0f}s ({rgb_secs/60:5.1f} min)")
        print(f"  oklab   {oklab_secs:7.0f}s ({oklab_secs/60:5.1f} min)  "
              f"{pct:+5.1f}% / {delta_s/60:+.1f} min vs rgb")
        print(f"  -> chose {chosen} (thresholds: "
              f"{args.oklab_threshold_pct:.0f}% or "
              f"{args.oklab_threshold_min:.0f} min)")
        info = generate(args.image, use_oklab=use_oklab,
                        write_outputs=True, **common)
    else:
        info = generate(args.image, use_oklab=(args.quant == "oklab"),
                        write_outputs=True, **common)

    print(f"Source:           {info['source']} -> {info['size']}x{info['size']}")
    print(f"Coverage:         {info['cells_covered']}/{info['canvas_cells']} "
          f"({100*info['cells_covered']/info['canvas_cells']:.1f}%)")
    print(f"Stamps:           {info['stamps']}")
    print(f"Color switches:   {info['color_switches']}")
    print(f"Brush switches:   {info['brush_switches']}  ({','.join(info['brushes'])})")
    if info["bucket_color"] is not None:
        print(f"Bucket fills:     {info['bucket_fills']}  "
              f"(color idx {info['bucket_color']})")
    print(f"Button presses:   {info['button_presses']}")
    secs = info["estimated_seconds"]
    print(f"Estimated runtime: {secs:.0f}s ({secs/60:.1f} min)")
    print(f"Macro:            {info['macro_path']}")


if __name__ == "__main__":
    main()
