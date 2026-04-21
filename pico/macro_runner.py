"""Parse + execute a macro.txt file by emitting HORIPAD S HID reports.

Absolute-deadline executor: every event fires at a deadline computed from the
run's start time, not relative to the previous event. A late event dispatch
doesn't shift later events — they keep their own absolute deadlines, so
jitter doesn't compound across a long macro.

The progress callback can raise to abort (e.g. ui.MacroAbort on B-press).
The finally block guarantees NEUTRAL is sent so inputs don't stay latched.

Timing precision notes:
  * GC is disabled across the main loop and collected explicitly between lines
    so a multi-ms GC stall can't fire mid-HOLD.
  * time.sleep() on CircuitPython quantizes to whole ms and rounds DOWN.
    _wait_until sleeps the bulk then busy-waits the final ~2ms against an
    absolute deadline; RP2350's timer is 1μs-resolution.
  * When TIMING_STREAM is True each HOLD prints a CSV row to serial
    (idx,req_us,actual_us,delta_us,press_skew_us). Capture via the CDC
    console — no filesystem or buffer allocation involved. The host USB
    stack polls HID at the same 8ms cadence whether or not anything is
    plugged into the Switch, so measurements are valid over USB alone.
"""
import gc
import os
import time

from horipad_hid import BTN, HAT, HAT_NEUTRAL, STICK_DIR, stick_bytes, report, NEUTRAL


NS_PER_S = 1_000_000_000
NS_PER_US = 1_000
NS_PER_MS = 1_000_000


# V2 compact macro format — emitted by convert.py's to_compact(). Each press
# is a single short token; a bare integer is a pause in milliseconds; `TOK*N`
# repeats the press N times. Default press = 34ms, default post-press gap =
# 33ms. Distinct from v1 (verbose `TOK 0.034s\n0.033s\n`): v2 lines never
# end with 's', so detection is per-line. Handcrafted macros (timing tests,
# accel tests) still use v1 and round-trip through the same runner.
V2_DPAD_HAT = {
    "R": "DPAD_RIGHT", "L": "DPAD_LEFT",
    "U": "DPAD_UP", "D": "DPAD_DOWN",
    "UR": "DPAD_UP_RIGHT", "UL": "DPAD_UP_LEFT",
    "DR": "DPAD_DOWN_RIGHT", "DL": "DPAD_DOWN_LEFT",
}
V2_STAMP_HAT = {k.lower(): v for k, v in V2_DPAD_HAT.items()}
V2_BTN = ("A", "B", "X", "Y")
V2_PRESS_NS = 34 * NS_PER_MS
V2_GAP_NS = 33 * NS_PER_MS
V2_CYCLE_NS = V2_PRESS_NS + V2_GAP_NS  # per-press advance


# V3 binary format — .mz files (uncompressed byte stream from convert.py
# to_binary_v3). Opcodes (class = byte & 0xE0):
#   0x00-0x13 single press (34ms), idx = byte & 0x1F
#   0x20-0x33 repeat press (34ms), idx = byte & 0x1F, next byte = count (1-255)
#   0x40-0x53 long-press,           idx = byte & 0x1F, next byte = duration ms
#   0x80      pause,                next 2 bytes BE = pause ms (0-65535)
# Token table matches V3_TOKENS in convert.py — do not reorder.
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


# Brush-select preamble: every macro assumes the drawing brush is 1x1. On a
# fresh item file the brush cursor parks on the top-right "null" (big circle,
# unusable). Navigate null (2, 0) → 1x1 (0, 1) before executing the macro body
# so handcrafted timing macros and convert.py output can both rely on it.
# Convention: user must open the item file fresh (or not touch the brush menu)
# before pressing Start. Mirrors the 300ms POST_PALETTE_OPEN and 1s
# POST_PALETTE_GAP buffers that palette nav in convert.py uses.
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

# Alternate preamble that lands on 3x3 instead of 1x1 — saves one brush-menu
# cycle (~2.5s) on macros whose first stamp uses 3x3. Selected via header
# version byte 0x02.
BRUSH_PREAMBLE_3X3 = (
    "X 0.1s",
    "0.2s",
    "X 0.1s",
    "0.2s",
    "0.3s",
    "DPAD_LEFT 0.1s",  # null (2,0) → (1,0)
    "0.2s",
    "DPAD_DOWN 0.1s",  # (1,0) → 3x3 (1,1)
    "0.2s",
    "A 0.1s",
    "0.2s",
    "0.3s",
    "B 0.1s",
    "0.2s",
    "1.0s",
)
# Plus-brush variant: null (2,0) → plus (1,0) is one LEFT. Selected via
# header version byte 0x03.
BRUSH_PREAMBLE_PLUS = (
    "X 0.1s",
    "0.2s",
    "X 0.1s",
    "0.2s",
    "0.3s",
    "DPAD_LEFT 0.1s",  # null (2,0) → plus (1,0)
    "0.2s",
    "A 0.1s",
    "0.2s",
    "0.3s",
    "B 0.1s",
    "0.2s",
    "1.0s",
)
BRUSH_PREAMBLE = BRUSH_PREAMBLE_1X1  # back-compat alias


# Stream a CSV row per HOLD to serial. Zero buffering, zero allocation —
# accumulating samples in an array exhausts heap under gc.disable().
TIMING_STREAM = False

# Sample index, reset per run. Module-level so the HOLD branch can bump it
# without threading state through every _exec_line_at call.
_t_idx = 0


# Cancel callback set per run by run_macro. Module-level so every
# _wait_until call picks it up without threading a param through all the
# _exec_line_at branches. Called every ~20ms during the sleep phase so
# B-abort lands inside a long HOLD; must be cheap (no display refresh).
_cancel_cb = None


def _wait_until(deadline_ns):
    """Sleep then busy-wait until monotonic_ns() >= deadline_ns.

    Plain busy-wait allocates a bigint on every monotonic_ns() call; over
    a 1-second wait that's tens of thousands of allocations which exhaust
    heap under gc.disable(). Hybrid approach: time.sleep() covers the bulk
    (sub-ms rounding doesn't matter when we're still >3ms from deadline),
    busy-wait only the final ~2ms where precision matters.

    `cancel_cb` (if given) is invoked every ~20ms during the sleep phase
    so B-abort can land inside a long HOLD instead of waiting for the
    next macro line. It must be cheap — no display work — or it'll steal
    time from the busy-wait and stretch the release edge past the next
    USB poll. The callback raises (MacroAbort) to abort.

    If we're already past the deadline (stalled), returns immediately —
    the event fires late, but subsequent events keep their own absolute
    deadlines so drift doesn't compound.
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


def _gc_sync(t_cursor):
    """Run GC and rebase t_cursor to wall clock if GC outran the planned gap.

    Without rebasing, a GC stall past `t_cursor` leaves the next `_wait_until`
    firing immediately — the NEXT press then gets a shortened hold (because
    its release deadline was computed from the old t_cursor). Rebasing keeps
    press duration at the designed 34/67/100ms even when GC steals time; the
    only cost is cadence slip, which is invisible to the Switch.
    """
    gc.collect()
    now = time.monotonic_ns()
    return now if now > t_cursor else t_cursor


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


def _run_v3(dev, f, total_bytes, progress_cb, t_cursor):
    """Stream-parse an uncompressed v3 binary file and execute opcodes.

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

    progress_cb fires every 64 opcodes and is followed by its own
    gc.collect() because display.refresh() allocates framebuffer data.
    """
    buf = V3_READ_BUF
    pos = 0
    end = f.readinto(buf)
    # Optional 8-byte header (magic "MZ1" + version + estimated_ms BE). If
    # present, skip it; headerless .mz files still execute. Version byte
    # 0x01 = land on 1x1 in preamble, 0x02 = land on 3x3 (run_macro handles
    # preamble selection before we get here — we just skip the bytes).
    if end >= 8 and buf[0] == 0x4D and buf[1] == 0x5A and buf[2] == 0x31:
        pos = 8
    a_val = BTN["A"]
    byte_pos = 0
    while True:
        if pos >= end:
            end = f.readinto(buf)
            pos = 0
            if end == 0:
                break
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
    # Pick preamble based on .mz header version byte. 0x02 = convert.py
    # determined the first stamp uses 3x3, so landing on 3x3 directly skips
    # one brush-menu cycle. Any other value (including headerless .mz and
    # all .txt macros) uses the default 1x1 preamble.
    preamble = BRUSH_PREAMBLE_1X1
    if is_binary:
        with open(macro_path, "rb") as _hf:
            _hdr = _hf.read(8)
        if (len(_hdr) < 8 or _hdr[0] != 0x4D or _hdr[1] != 0x5A
                or _hdr[2] != 0x31):
            raise RuntimeError("bad .mz header (expected MZ1 magic)")
        if _hdr[3] not in (0x01, 0x02, 0x03):
            raise RuntimeError("unsupported .mz version: 0x%02x" % _hdr[3])
        if _hdr[3] == 0x02:
            preamble = BRUSH_PREAMBLE_3X3
        elif _hdr[3] == 0x03:
            preamble = BRUSH_PREAMBLE_PLUS
    start = time.monotonic()
    if TIMING_STREAM:
        print("T_HEADER,idx,req_us,actual_us,delta_us,press_skew_us")
    try:
        t_cursor = time.monotonic_ns()
        gc.disable()
        for line in preamble:
            t_cursor = _exec_line_at(dev, line, t_cursor)
        if is_binary:
            total_bytes = os.stat(macro_path)[6]
            with open(macro_path, "rb") as f:
                t_cursor = _run_v3(dev, f, total_bytes, progress_cb, t_cursor)
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
    # Success-only — aborts propagate through the finally above and never
    # reach here. Best-effort push; ntfy.send swallows its own errors.
    try:
        import ntfy
        stem = macro_path.rsplit("/", 1)[-1]
        for ext in (".mz", ".txt"):
            if stem.endswith(ext):
                stem = stem[:-len(ext)]
                break
        ntfy.send("Print complete: " + stem, title="Tomodachi Printer")
    except Exception as e:
        print("ntfy hook failed:", e)
    if progress_cb is not None:
        progress_cb(100, total or 1, total or 1)
    return time.monotonic() - start
