"""Parse + execute a macro by emitting HORIPAD S HID reports.

Absolute-deadline executor: every event fires at a deadline computed
from the run's start time, not relative to the previous event — late
dispatches don't shift later events, so jitter doesn't compound.

The progress callback can raise to abort. The finally block guarantees
NEUTRAL is sent so inputs don't stay latched.

GC is disabled across the main loop and collected explicitly between
events so a multi-ms GC stall can't fire mid-press.
"""
import gc
import os
import time

from horipad_hid import BTN, HAT, HAT_NEUTRAL, STICK_DIR, stick_bytes, report, NEUTRAL

NS_PER_S = 1_000_000_000
NS_PER_US = 1_000
NS_PER_MS = 1_000_000


# V2 compact text format: bare token = single press, `TOK*N` = repeat,
# bare integer = pause in ms. Lines never end with "s" — that's how we
# distinguish from the v1 verbose format that still flows through the
# same runner for handcrafted test macros.
V2_DPAD_HAT = {
    "R": "DPAD_RIGHT", "L": "DPAD_LEFT",
    "U": "DPAD_UP", "D": "DPAD_DOWN",
    "UR": "DPAD_UP_RIGHT", "UL": "DPAD_UP_LEFT",
    "DR": "DPAD_DOWN_RIGHT", "DL": "DPAD_DOWN_LEFT",
}
V2_STAMP_HAT = {k.lower(): v for k, v in V2_DPAD_HAT.items()}
V2_BTN = ("A", "B", "X", "Y")
# Exact 2-frame cycle at 30fps so we don't slip relative to the game
# frame clock and accidentally trip d-pad acceleration.
V2_CYCLE_NS = 2 * 33_333_333
V2_PRESS_NS = 34 * NS_PER_MS
V2_GAP_NS = V2_CYCLE_NS - V2_PRESS_NS


# V3 binary opcode format. Class = byte & 0xE0:
#   0x00 single press (34ms)        idx in low 5 bits
#   0x20 repeat press (34ms)        + 1 byte count
#   0x40 long-press                 + 1 byte duration_ms
#   0x80 pause                      + 2 bytes BE duration_ms
# Token table must match V3_TOKENS in convert.py — do not reorder.
V3_TOKENS = (
    "DPAD_RIGHT", "DPAD_LEFT", "DPAD_UP", "DPAD_DOWN",
    "DPAD_UP_RIGHT", "DPAD_UP_LEFT", "DPAD_DOWN_RIGHT", "DPAD_DOWN_LEFT",
    # Lowercase half = STAMP variants (indices 8-15)
    "DPAD_RIGHT", "DPAD_LEFT", "DPAD_UP", "DPAD_DOWN",
    "DPAD_UP_RIGHT", "DPAD_UP_LEFT", "DPAD_DOWN_RIGHT", "DPAD_DOWN_LEFT",
    # Buttons (16-19)
    "A", "B", "X", "Y",
)
V3_STAMP_START = 8
V3_BTN_START = 16
V3_OP_SINGLE = 0x00
V3_OP_REPEAT = 0x20
# Long-press: [0x40 | idx] [ms]. Holds the button for the encoded ms instead
# of the 34ms V2_PRESS_NS default — used for brush/palette menu presses, which
# drop frames intermittently at 34ms and leave the menu on the wrong selection.
V3_OP_LONG_PRESS = 0x40
V3_OP_PAUSE = 0x80


# Brush preamble for text macros. .mz files embed their own preamble
# (the runner is data-driven for .mz), so this is only run for .txt.
# Requires the brush cursor to start on "null" (fresh item file).
BRUSH_PREAMBLE_1X1 = (
    "X 0.1s",
    "0.2s",        # BRUSH_PAUSE between Xs (matches convert.py brush_select)
    "X 0.1s",
    "0.2s",        # BRUSH_PAUSE
    "0.3s",        # POST_PALETTE_OPEN — grid-open animation buffer
    "DPAD_LEFT 0.1s",
    "0.2s",
    "DPAD_LEFT 0.1s",
    "0.2s",
    "DPAD_DOWN 0.1s",
    "0.2s",
    "A 0.1s",      # select 1x1 (menu stays open)
    "0.2s",        # BRUSH_PAUSE
    "0.3s",        # POST_PALETTE_OPEN — post-select animation before B
    "B 0.1s",      # dismiss brush menu
    "0.2s",        # BRUSH_PAUSE
    "1.0s",        # POST_PALETTE_GAP — menu close animation
)


# Debug: stream a CSV row per HOLD to the CDC console for timing capture.
TIMING_STREAM = False

# Sample index, reset per run. Module-level so the HOLD branch can bump it
# without threading state through every _exec_line_at call.
_t_idx = 0


# Cancel callback set per run by run_macro. Called during the sleep
# phase so B-abort can land mid-HOLD; must be cheap (no display work).
_cancel_cb = None


def _wait_until(deadline_ns):
    """Sleep then busy-wait until monotonic_ns() >= deadline_ns.

    Hybrid approach: time.sleep() for the bulk, busy-wait the final
    ~2ms. Plain busy-wait allocates a bigint per call and exhausts the
    heap under gc.disable().
    """
    while True:
        remaining = deadline_ns - time.monotonic_ns()
        if remaining <= 3_000_000:
            break
        if _cancel_cb is not None:
            _cancel_cb()
        chunk_ns = remaining - 2_000_000
        if chunk_ns > 20_000_000:
            chunk_ns = 20_000_000
        time.sleep(chunk_ns / 1_000_000_000)
    while time.monotonic_ns() < deadline_ns:
        pass
    return time.monotonic_ns()


def _write(dev, data):
    dev.send(data)


_FRAME_NS = 33_333_333  # ~30fps frame at the Tomodachi lobby framerate


def _gc_sync(t_cursor):
    """Run GC and rebase t_cursor if GC overran.

    On overrun, advance t_cursor to the next frame-aligned boundary past the
    overrun rather than to "now". Keeps the post-GC press's phase consistent
    with where it would have landed without GC — avoids resuming at an
    arbitrary phase within a frame where the Switch's input poll could
    sample the press mid-transition.
    """
    gc.collect()
    now = time.monotonic_ns()
    if now <= t_cursor:
        return t_cursor
    overrun = now - t_cursor
    frames = (overrun + _FRAME_NS - 1) // _FRAME_NS  # ceiling
    # Guarantee at least 2 frames of headroom on a rebase so the next press's
    # release deadline (t_cursor + V2_PRESS_NS = 34ms) is always in the
    # future relative to the rebased t_cursor — defends against any edge
    # case where the rebase undershoots by less than a full press window.
    return t_cursor + max(frames, 2) * _FRAME_NS


def _count_lines(path):
    n = 0
    with open(path, "r") as f:
        for _ in f:
            n += 1
    return n


def _exec_v2(dev, line, t_cursor):
    """Execute one v2 compact line. Runs every press with the fixed v2 press +
    gap cadence. Returns the new absolute-time cursor.

    - `<TOK>`       single press, advance CYCLE_NS
    - `<TOK>*<n>`   n consecutive presses
    - `<ms>`        bare pause, advance ms * 1e6 ns
    """
    if line[0].isdigit():
        return t_cursor + int(line) * NS_PER_MS
    if "*" in line:
        tok, count = line.split("*")
        n = int(count)
    else:
        tok = line
        n = 1
    if tok in V2_BTN:
        btn_val = BTN[tok]
        for _ in range(n):
            _wait_until(t_cursor)
            _write(dev, report(buttons=btn_val))
            _wait_until(t_cursor + V2_PRESS_NS)
            _write(dev, NEUTRAL)
            t_cursor += V2_CYCLE_NS
        return t_cursor
    hat_name = V2_DPAD_HAT.get(tok)
    if hat_name is not None:
        hat_val = HAT[hat_name]
        for _ in range(n):
            _wait_until(t_cursor)
            _write(dev, report(hat=hat_val))
            _wait_until(t_cursor + V2_PRESS_NS)
            _write(dev, NEUTRAL)
            t_cursor += V2_CYCLE_NS
        return t_cursor
    stamp_hat = V2_STAMP_HAT.get(tok)
    if stamp_hat is not None:
        hat_val = HAT[stamp_hat]
        a_val = BTN["A"]
        for _ in range(n):
            _wait_until(t_cursor)
            _write(dev, report(buttons=a_val, hat=hat_val))
            _wait_until(t_cursor + V2_PRESS_NS)
            _write(dev, NEUTRAL)
            t_cursor += V2_CYCLE_NS
        return t_cursor
    print("unknown v2 token:", line)
    return t_cursor


def _exec_line_at(dev, line, t_cursor):
    """Execute `line` with events scheduled from absolute cursor `t_cursor` (ns).

    Each line consumes `duration_ns` of the schedule and returns the new
    cursor (= t_cursor + duration_ns). Sub-events fire at deadlines derived
    from t_cursor. Pure-sleep lines don't fire any event; they just advance
    the cursor so the next line's first _wait_until absorbs the gap.
    """
    if line and not line.endswith("s"):
        return _exec_v2(dev, line, t_cursor)
    toks = line.split()
    n = len(toks)
    if n == 0:
        return t_cursor

    if (n == 2 and toks[0].startswith("HOLD_")
            and toks[0][5:] in HAT and toks[1].endswith("s")):
        global _t_idx
        hat_tok = toks[0][5:]
        dur_ns = int(float(toks[1][:-1]) * NS_PER_S)
        t_press = t_cursor
        t_release = t_cursor + dur_ns
        fire_press = _wait_until(t_press)
        _write(dev, report(hat=HAT[hat_tok]))
        fire_release = _wait_until(t_release)
        _write(dev, NEUTRAL)
        if TIMING_STREAM:
            req = dur_ns // NS_PER_US
            act = (fire_release - fire_press) // NS_PER_US
            skew = (fire_press - t_press) // NS_PER_US
            print("T,{},{},{},{},{}".format(_t_idx, req, act, act - req, skew))
            _t_idx += 1
        return t_release

    if (n == 2 and toks[0].startswith("STAMP_")
            and toks[0][6:] in HAT and toks[1].endswith("s")):
        hat_tok = toks[0][6:]
        dur_ns = int(float(toks[1][:-1]) * NS_PER_S)
        _wait_until(t_cursor)
        _write(dev, report(buttons=BTN["A"], hat=HAT[hat_tok]))
        _wait_until(t_cursor + dur_ns)
        _write(dev, NEUTRAL)
        return t_cursor + dur_ns

    if n == 2 and toks[1].endswith("s"):
        tok = toks[0]
        dur_ns = int(float(toks[1][:-1]) * NS_PER_S)
        if tok in BTN:
            _wait_until(t_cursor)
            _write(dev, report(buttons=BTN[tok]))
            _wait_until(t_cursor + dur_ns)
            _write(dev, NEUTRAL)
            return t_cursor + dur_ns
        if tok in HAT:
            _wait_until(t_cursor)
            _write(dev, report(hat=HAT[tok]))
            _wait_until(t_cursor + dur_ns)
            _write(dev, NEUTRAL)
            return t_cursor + dur_ns
        print("unknown token:", tok)
        return t_cursor

    if (n == 3 and toks[0] in STICK_DIR
            and toks[2].endswith("s")):
        magnitude = float(toks[1])
        dur_ns = int(float(toks[2][:-1]) * NS_PER_S)
        lx, ly = stick_bytes(toks[0], magnitude)
        _wait_until(t_cursor)
        _write(dev, report(lx=lx, ly=ly))
        _wait_until(t_cursor + dur_ns)
        _write(dev, NEUTRAL)
        return t_cursor + dur_ns

    if (toks[0].startswith("DRAW_") and toks[0][5:]
            in ("RIGHT", "LEFT", "UP", "DOWN")):
        direction = "LSTICK_" + toks[0][5:]
        magnitude = float(toks[1])
        dur_ns = int(float(toks[2][:-1]) * NS_PER_S)
        lx, ly = stick_bytes(direction, magnitude)
        _wait_until(t_cursor)
        _write(dev, report(lx=lx, ly=ly, buttons=BTN["A"]))
        _wait_until(t_cursor + dur_ns)
        _write(dev, NEUTRAL)
        return t_cursor + dur_ns

    if (toks[0].startswith("SCAN_") and toks[0][5:]
            in ("RIGHT", "LEFT", "UP", "DOWN")):
        direction = "LSTICK_" + toks[0][5:]
        magnitude = float(toks[1])
        dur_ns = int(float(toks[2][:-1]) * NS_PER_S)
        a_offsets_ns = sorted(
            int(float(t[:-1]) * NS_PER_S) for t in toks[3:] if t.endswith("s")
        )
        lx, ly = stick_bytes(direction, magnitude)
        _wait_until(t_cursor)
        _write(dev, report(lx=lx, ly=ly))
        for a_off in a_offsets_ns:
            t_a = t_cursor + a_off
            _wait_until(t_a)
            _write(dev, report(lx=lx, ly=ly, buttons=BTN["A"]))
            _wait_until(t_a + 34_000_000)
            _write(dev, report(lx=lx, ly=ly))
        _wait_until(t_cursor + dur_ns)
        _write(dev, NEUTRAL)
        return t_cursor + dur_ns

    # HOLD_SCAN_DPAD_<dir> <dur>s <a_off>s [<a_off>s...] — hold the d-pad
    # for dur_ns and fire A stamps at the given offsets. The hat state
    # stays asserted during each A tap (we re-send hat+A then hat) so the
    # game's hold-repeat shouldn't reset. Tests whether stamp-while-moving
    # is viable as a primitive.
    if (toks[0].startswith("HOLD_SCAN_DPAD_")
            and toks[0][15:] in ("RIGHT", "LEFT", "UP", "DOWN")
            and n >= 2 and toks[1].endswith("s")):
        hat_val = HAT["DPAD_" + toks[0][15:]]
        dur_ns = int(float(toks[1][:-1]) * NS_PER_S)
        a_offsets_ns = sorted(
            int(float(t[:-1]) * NS_PER_S) for t in toks[2:] if t.endswith("s")
        )
        _wait_until(t_cursor)
        _write(dev, report(hat=hat_val))
        for a_off in a_offsets_ns:
            t_a = t_cursor + a_off
            _wait_until(t_a)
            _write(dev, report(hat=hat_val, buttons=BTN["A"]))
            _wait_until(t_a + 25_000_000)
            _write(dev, report(hat=hat_val))
        _wait_until(t_cursor + dur_ns)
        _write(dev, NEUTRAL)
        return t_cursor + dur_ns

    if n == 1 and toks[0].endswith("s"):
        dur_ns = int(float(toks[0][:-1]) * NS_PER_S)
        return t_cursor + dur_ns

    print("skip:", line)
    return t_cursor


V3_READ_BUF = bytearray(512)  # module-level: allocate once, never resize.


def _run_v3(dev, f, total_bytes, progress_cb, t_cursor, macro_limit=None):
    """Stream-parse an uncompressed v3 opcode stream and execute it.

    The caller is responsible for seeking `f` to the start of the opcode
    bytes — this loop reads forward from the current position and stops
    at `macro_limit` bytes (so MZ2's trailing BMP / MZ3's preceding BMP
    don't get parsed as opcodes).

    Reads into a pre-allocated bytearray (`V3_READ_BUF`) via `readinto()` —
    zero per-byte allocations. Tracks (pos, end) into the buffer and refills
    when exhausted. Opcodes that span a refill boundary work because we
    check-refill before each byte access, not per-opcode.

    gc.collect() fires after EVERY press (inside the REPEAT inner loops too),
    matching the v1 text path's per-event cadence. _wait_until's busy-wait
    allocates a bigint per iteration — under gc.disable() those pile up fast
    (many KB per press). A REPEAT op can pack 100+ presses, so per-opcode GC
    isn't enough; we need per-press. The GC window fits inside the 33ms
    post-release gap so press cadence is unaffected.
    """
    buf = V3_READ_BUF
    pos = 0
    end = f.readinto(buf)
    if macro_limit is not None and end > macro_limit:
        end = macro_limit
    a_val = BTN["A"]
    byte_pos = 0
    while True:
        if pos >= end:
            end = f.readinto(buf)
            pos = 0
            if end == 0:
                break
            if macro_limit is not None:
                remaining = macro_limit - byte_pos
                if remaining <= 0:
                    break
                if end > remaining:
                    end = remaining
        b = buf[pos]; pos += 1; byte_pos += 1
        cls = b & 0xE0
        if cls == V3_OP_SINGLE:
            idx = b & 0x1F
            name = V3_TOKENS[idx]
            if idx >= V3_BTN_START:
                btn_val = BTN[name]
                _wait_until(t_cursor)
                _write(dev, report(buttons=btn_val))
                _wait_until(t_cursor + V2_PRESS_NS)
                _write(dev, NEUTRAL)
            elif idx >= V3_STAMP_START:
                hat_val = HAT[name]
                _wait_until(t_cursor)
                _write(dev, report(buttons=a_val, hat=hat_val))
                _wait_until(t_cursor + V2_PRESS_NS)
                _write(dev, NEUTRAL)
            else:
                hat_val = HAT[name]
                _wait_until(t_cursor)
                _write(dev, report(hat=hat_val))
                _wait_until(t_cursor + V2_PRESS_NS)
                _write(dev, NEUTRAL)
            t_cursor += V2_CYCLE_NS
            t_cursor = _gc_sync(t_cursor)
        elif cls == V3_OP_REPEAT:
            idx = b & 0x1F
            if pos >= end:
                end = f.readinto(buf); pos = 0
                if end == 0:
                    raise RuntimeError("truncated v3 repeat count")
            count = buf[pos]; pos += 1; byte_pos += 1
            name = V3_TOKENS[idx]
            if idx >= V3_BTN_START:
                btn_val = BTN[name]
                for _ in range(count):
                    _wait_until(t_cursor)
                    _write(dev, report(buttons=btn_val))
                    _wait_until(t_cursor + V2_PRESS_NS)
                    _write(dev, NEUTRAL)
                    t_cursor += V2_CYCLE_NS
                    t_cursor = _gc_sync(t_cursor)
            elif idx >= V3_STAMP_START:
                hat_val = HAT[name]
                for _ in range(count):
                    _wait_until(t_cursor)
                    _write(dev, report(buttons=a_val, hat=hat_val))
                    _wait_until(t_cursor + V2_PRESS_NS)
                    _write(dev, NEUTRAL)
                    t_cursor += V2_CYCLE_NS
                    t_cursor = _gc_sync(t_cursor)
            else:
                hat_val = HAT[name]
                for _ in range(count):
                    _wait_until(t_cursor)
                    _write(dev, report(hat=hat_val))
                    _wait_until(t_cursor + V2_PRESS_NS)
                    _write(dev, NEUTRAL)
                    t_cursor += V2_CYCLE_NS
                    t_cursor = _gc_sync(t_cursor)
        elif cls == V3_OP_LONG_PRESS:
            idx = b & 0x1F
            if pos >= end:
                end = f.readinto(buf); pos = 0
                if end == 0:
                    raise RuntimeError("truncated v3 long-press ms")
            press_ms = buf[pos]; pos += 1; byte_pos += 1
            press_ns = press_ms * NS_PER_MS
            name = V3_TOKENS[idx]
            if idx >= V3_BTN_START:
                btn_val = BTN[name]
                _wait_until(t_cursor)
                _write(dev, report(buttons=btn_val))
                _wait_until(t_cursor + press_ns)
                _write(dev, NEUTRAL)
            elif idx >= V3_STAMP_START:
                hat_val = HAT[name]
                _wait_until(t_cursor)
                _write(dev, report(buttons=a_val, hat=hat_val))
                _wait_until(t_cursor + press_ns)
                _write(dev, NEUTRAL)
            else:
                hat_val = HAT[name]
                _wait_until(t_cursor)
                _write(dev, report(hat=hat_val))
                _wait_until(t_cursor + press_ns)
                _write(dev, NEUTRAL)
            t_cursor += press_ns + V2_GAP_NS
            t_cursor = _gc_sync(t_cursor)
        elif b == V3_OP_PAUSE:
            if pos >= end:
                end = f.readinto(buf); pos = 0
                if end == 0:
                    raise RuntimeError("truncated v3 pause (hi)")
            hi = buf[pos]; pos += 1; byte_pos += 1
            if pos >= end:
                end = f.readinto(buf); pos = 0
                if end == 0:
                    raise RuntimeError("truncated v3 pause (lo)")
            lo = buf[pos]; pos += 1; byte_pos += 1
            t_cursor += ((hi << 8) | lo) * NS_PER_MS
            # Absorb the pause in-place so progress_cb fires with the updated
            # byte_pos after it (instead of after the next press), keeping the
            # Pause (Y) button responsive across long POST_PALETTE_GAPs. Abort
            # still lands inside the sleep via _cancel_cb.
            _wait_until(t_cursor)
        else:
            print("v3 unknown opcode: 0x%02x at %d" % (b, byte_pos - 1))
        if progress_cb is not None:
            pct = int(100 * byte_pos / total_bytes) if total_bytes else 100
            # progress_cb may return a pause duration (ns) to shift the
            # absolute-deadline scheduler forward by — used for the pause
            # button, which runs a blocking passthrough loop inside the
            # callback. 0 / None means "no pause, proceed".
            pause_ns = progress_cb(pct, byte_pos, total_bytes) or 0
            if pause_ns:
                t_cursor += pause_ns
            t_cursor = _gc_sync(t_cursor)
    return t_cursor


def run_macro(dev, macro_path, progress_cb=None, cancel_cb=None):
    """Execute the macro at `macro_path`. `progress_cb(pct, line_index, total)`
    is called once per macro line — the UI uses it to redraw the progress
    bar and check abort. `cancel_cb()` is called every ~20ms during long
    HOLDs so B-abort lands promptly; it must be cheap (no display work).
    Both callbacks raise (MacroAbort) to abort; the finally block parks
    inputs so nothing stays latched.

    The macro generator assumes the in-game canvas cursor is parked at
    (128, 128) — user does that manually before pressing Start. The brush is
    auto-selected to 1x1 by the BRUSH_PREAMBLE at run start; user must open
    the item file fresh so the brush cursor begins on "null".

    Dispatch: `.mz` files are uncompressed v3 binary (stream-parsed); every
    other extension is parsed line-by-line as text (v1 verbose or v2 compact,
    detected per-line). Binary is streamed one opcode at a time so large
    macros don't have to fit in RAM at once.
    """
    global _t_idx, _cancel_cb
    _t_idx = 0
    _cancel_cb = cancel_cb
    gc.collect()
    is_binary = macro_path.endswith(".mz")
    total = 0 if is_binary else _count_lines(macro_path)
    macro_limit = None
    opcode_start = 0  # byte offset of first opcode byte inside the .mz
    if is_binary:
        with open(macro_path, "rb") as _hf:
            _sniff = _hf.read(6)
        if len(_sniff) < 2:
            raise RuntimeError("empty .mz file")
        if _sniff[0] == 0x42 and _sniff[1] == 0x4D:
            # MZ3 layout: BMP first (offsets 0..bfSize), then "MZ3"+header
            # + opcode stream. bfSize at bytes 2..5 of the BMP header.
            bmp_size = _sniff[2] | (_sniff[3] << 8) | (_sniff[4] << 16) | (_sniff[5] << 24)
            with open(macro_path, "rb") as _hf:
                _hf.seek(bmp_size)
                _trailer = _hf.read(12)
            if (len(_trailer) < 12 or _trailer[0] != 0x4D or _trailer[1] != 0x5A
                    or _trailer[2] != 0x33):
                raise RuntimeError("bad MZ3 trailer (expected MZ3 magic)")
            brush_byte = _trailer[3] & 0x0F
            if brush_byte not in (0x01, 0x02, 0x03, 0x04):
                raise RuntimeError("unsupported MZ3 version: 0x%02x" % _trailer[3])
            macro_limit = ((_trailer[8] << 24) | (_trailer[9] << 16)
                           | (_trailer[10] << 8) | _trailer[11])
            opcode_start = bmp_size + 12
        elif _sniff[0] == 0x4D and _sniff[1] == 0x5A:
            # MZ1/MZ2 (legacy): header first, opcodes after. MZ2 has a BMP
            # appended past the opcode stream.
            with open(macro_path, "rb") as _hf:
                _hdr = _hf.read(16)
            if _hdr[2] not in (0x31, 0x32):
                raise RuntimeError("unsupported .mz magic: %s" % _hdr[:3])
            brush_byte = _hdr[3] & 0x0F
            if brush_byte not in (0x01, 0x02, 0x03, 0x04):
                raise RuntimeError("unsupported .mz version: 0x%02x" % _hdr[3])
            if _hdr[2] == 0x32 and len(_hdr) >= 16:
                macro_limit = ((_hdr[8] << 24) | (_hdr[9] << 16)
                               | (_hdr[10] << 8) | _hdr[11])
                opcode_start = 16
            elif _hdr[2] == 0x31:
                opcode_start = 8
        else:
            raise RuntimeError("unrecognized .mz magic: %s" % _sniff[:2])
    start = time.monotonic()
    if TIMING_STREAM:
        print("T_HEADER,idx,req_us,actual_us,delta_us,press_skew_us")
    try:
        t_cursor = time.monotonic_ns()
        gc.disable()
        # .mz files embed their own brush preamble at the head of the opcode
        # stream (convert.py emits it). .txt macros don't — fall back to the
        # runner-side prelude so handcrafted timing tests still land on 1x1.
        if not is_binary:
            for line in BRUSH_PREAMBLE_1X1:
                t_cursor = _exec_line_at(dev, line, t_cursor)
        if is_binary:
            file_bytes = os.stat(macro_path)[6]
            total_bytes = macro_limit if macro_limit is not None else file_bytes
            with open(macro_path, "rb") as f:
                f.seek(opcode_start)
                t_cursor = _run_v3(dev, f, total_bytes, progress_cb, t_cursor,
                                    macro_limit=macro_limit)
        else:
            with open(macro_path, "r") as f:
                for i, raw in enumerate(f):
                    line = raw.strip()
                    if not line:
                        continue
                    if progress_cb is not None:
                        pct = int(100 * i / total) if total else 100
                        pause_ns = progress_cb(pct, i, total) or 0
                        if pause_ns:
                            t_cursor += pause_ns
                    t_cursor = _exec_line_at(dev, line, t_cursor)
                    t_cursor = _gc_sync(t_cursor)
    finally:
        _write(dev, NEUTRAL)
        gc.enable()
        _cancel_cb = None
    # Push the progress bar to 100% before returning. Post-completion work
    # (notifications, etc.) is the UI's responsibility — it can swap the
    # status text around those calls without macro_runner having to know.
    if progress_cb is not None:
        progress_cb(100, total or 1, total or 1)
    return time.monotonic() - start
