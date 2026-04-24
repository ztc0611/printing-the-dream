"""On-device UI state machine.

States:
  GRID     - pick a macro from the list. A=confirm, Y=reboot to USB drive.
  SETUP    - HID passthrough so you can position the cursor on the Switch
             with the Pico's joystick/buttons. Y=start print, X=back.
  RUNNING  - progress bar. B=abort (raises MacroAbort from progress_cb).

The display is optional — if display is None (lib missing), UI still runs
and prints state transitions to the console so the rest of the firmware
can be exercised over serial.

Layout on 240x240:
  left area (0..176)  : title + scrollable list or passthrough readout
  right column (180..240) : 4 context-sensitive button labels (A/B/X/Y)
"""
import gc
import time

import displayio
import microcontroller
import terminalio
from adafruit_display_text import label

import config
import macro_runner
from horipad_hid import BTN, HAT, HAT_NEUTRAL


STATE_GRID = 0
STATE_SETUP = 1
STATE_RUNNING = 2


# Magic byte written to microcontroller.nvm[0] by _reboot_to_usb_drive.
# boot.py reads this on next boot; if present, enables MSC regardless of
# BTN_Y state, then clears the flag so the following boot returns to
# normal HID-only mode.
NVM_USB_DRIVE_FLAG = 0xA5


LIST_ROW_H = 16
LIST_ROWS_VISIBLE = 12
LIST_X = 6
LIST_Y0 = 24

BTN_X = 186
BTN_YS = (22, 82, 142, 202)  # aligned with physical A/B/X/Y buttons

# GRID-state thumbnail layout. 2 cols × 2 rows of 72×72 tiles with a
# macro-name label under each. Convert.py writes <stem>_macro.bmp
# alongside <stem>_macro.mz; .txt macros have no thumbnail and fall back
# to a "?" placeholder tile.
TILE_SIZE = 72
TILE_GAP_X = 10
TILE_GAP_Y = 10
TILE_LABEL_H = 14
TILE_COLS = 2
TILE_ROWS_VIS = 2
TILE_AREA_W = TILE_COLS * TILE_SIZE + (TILE_COLS - 1) * TILE_GAP_X
# Left edge of the thumbnail area. 60px reserved on the right for button labels.
TILE_X0 = (config.LCD_WIDTH - 60 - TILE_AREA_W) // 2 + 2
TILE_Y0 = 24
TILE_ROW_PITCH = TILE_SIZE + TILE_LABEL_H + TILE_GAP_Y
TILE_HL_INSET = 2  # border width around selected tile

# Progress bar geometry (RUNNING state). Bar is a full-width filled bitmap
# with a black "cover" TileGrid on top that slides right as pct climbs —
# O(1) per frame, no per-pixel fills during the print.
BAR_X = 6
BAR_Y = 76
BAR_W = 170
BAR_H = 16
BAR_HIDDEN_Y = -100

COLOR_BG = 0x000000
COLOR_FG = 0xFFFFFF
COLOR_DIM = 0x808080
COLOR_HL_BG = 0x0060C0
COLOR_HL_FG = 0xFFFFFF
COLOR_ACCENT = 0x00FF88
COLOR_WARN = 0xFFCC00
COLOR_ERR = 0xFF4040

# Idle-dim thresholds. Wake presses are consumed; release+repress required.
DIM_IDLE_MS = 60_000
DIM_BRIGHTNESS = 0.08
FULL_BRIGHTNESS = 1.0


class MacroAbort(Exception):
    """Raised from the progress callback when the user presses B."""


class UI:
    def __init__(self, display, inputs, pad, list_macros_fn):
        self.display = display
        self.inputs = inputs
        self.pad = pad
        self._list_macros = list_macros_fn

        self.state = STATE_GRID
        self.macros = []
        self.selected = 0
        self.scroll = 0
        self.status_text = ""

        self._last_activity_ms = time.monotonic_ns() // 1_000_000
        self._dimmed = False
        self._wake_blocked = set()

        self._build()
        self._refresh_macros()
        self._render()

    # ------------------------------------------------------------------
    # build: one persistent Group with labels we mutate in place.
    # ------------------------------------------------------------------
    def _build(self):
        if self.display is None:
            self._title = None
            self._rows = []
            self._btns = []
            self._status = None
            return

        root = displayio.Group()

        # Title bar
        self._title = label.Label(
            terminalio.FONT, text="", color=COLOR_ACCENT, x=LIST_X, y=10,
        )
        root.append(self._title)

        # Highlight bar behind the selected row. Fixed-size 1bpp bitmap;
        # we just change its y on selection. value_count=1 means every pixel
        # maps to palette[0] (solid fill).
        hl_bitmap = displayio.Bitmap(176, LIST_ROW_H, 1)
        hl_palette = displayio.Palette(1)
        hl_palette[0] = COLOR_HL_BG
        self._hl = displayio.TileGrid(
            hl_bitmap, pixel_shader=hl_palette, x=2, y=-100,
        )
        root.append(self._hl)

        # GRID-state thumbnail highlight: a (tile+border) filled bitmap that
        # sits behind the selected tile so ~2px of accent shows around it.
        grid_hl_bmp = displayio.Bitmap(
            TILE_SIZE + 2 * TILE_HL_INSET, TILE_SIZE + 2 * TILE_HL_INSET, 1,
        )
        grid_hl_pal = displayio.Palette(1)
        grid_hl_pal[0] = COLOR_ACCENT
        self._grid_hl = displayio.TileGrid(
            grid_hl_bmp, pixel_shader=grid_hl_pal, x=0, y=-200,
        )
        root.append(self._grid_hl)

        # Thumbnails for the GRID state. One sub-group per visible slot so
        # we can rebuild individual slots on navigation without redrawing
        # tiles that didn't change. _thumb_last tracks the macro currently
        # rendered in each slot so _render_grid can skip untouched ones.
        self._thumb_group = displayio.Group()
        root.append(self._thumb_group)
        self._thumb_slots = []
        for _ in range(TILE_COLS * TILE_ROWS_VIS):
            g = displayio.Group()
            self._thumb_group.append(g)
            self._thumb_slots.append(g)
        self._thumb_last = [None] * (TILE_COLS * TILE_ROWS_VIS)

        # List rows (text)
        self._rows = []
        for i in range(LIST_ROWS_VISIBLE):
            lbl = label.Label(
                terminalio.FONT, text="", color=COLOR_FG,
                x=LIST_X, y=LIST_Y0 + i * LIST_ROW_H,
            )
            self._rows.append(lbl)
            root.append(lbl)

        # Progress bar (RUNNING only). fill is the full-width accent bar;
        # cover is a black rect we slide right to reveal fill from the left.
        fill_bmp = displayio.Bitmap(BAR_W, BAR_H, 1)
        fill_pal = displayio.Palette(1)
        fill_pal[0] = COLOR_ACCENT
        self._bar_fill = displayio.TileGrid(
            fill_bmp, pixel_shader=fill_pal, x=BAR_X, y=BAR_HIDDEN_Y,
        )
        root.append(self._bar_fill)

        cover_bmp = displayio.Bitmap(BAR_W, BAR_H, 1)
        cover_pal = displayio.Palette(1)
        cover_pal[0] = COLOR_BG
        self._bar_cover = displayio.TileGrid(
            cover_bmp, pixel_shader=cover_pal, x=BAR_X, y=BAR_HIDDEN_Y,
        )
        root.append(self._bar_cover)

        # SETUP-state preview group: one tile-sized thumbnail rebuilt when
        # the selected macro changes. Off-screen until _render_setup places
        # it. Separate from the GRID thumb_group so state transitions don't
        # fight over the same display list.
        self._setup_preview = displayio.Group()
        root.append(self._setup_preview)
        self._setup_preview_last = None

        # Status line at bottom
        self._status = label.Label(
            terminalio.FONT, text="", color=COLOR_DIM, x=LIST_X, y=228,
        )
        root.append(self._status)

        # Button labels (right column), right-aligned against the screen edge
        # so variable-width text stays flush with the physical buttons.
        self._btns = []
        for y in BTN_YS:
            lbl = label.Label(
                terminalio.FONT, text="", color=COLOR_FG,
                anchor_point=(1.0, 0.5),
                anchored_position=(config.LCD_WIDTH - 4, y),
            )
            self._btns.append(lbl)
            root.append(lbl)

        self.display.root_group = root

    # ------------------------------------------------------------------
    # state transitions
    # ------------------------------------------------------------------
    def _enter(self, new_state):
        self.state = new_state
        if new_state == STATE_GRID:
            self._refresh_macros()
        elif new_state == STATE_SETUP:
            # Buttons held at transition time (A from the "run" press, CTRL
            # from a joystick-click entry, etc.) must be released before
            # passthrough forwards them — otherwise the A that *selected* the
            # macro leaks straight through to the Switch. Each blocked button
            # clears as soon as we observe it released.
            self._setup_blocked = {
                k for k in ("A", "B", "CTRL") if self.inputs.held.get(k)
            }
        self._render()

    def _refresh_macros(self):
        try:
            self.macros = self._list_macros()
        except Exception as e:
            print("list_macros failed:", e)
            self.macros = []
        if self.selected >= len(self.macros):
            self.selected = max(0, len(self.macros) - 1)
        # In grid mode self.scroll is a ROW index (2 tiles per row). Pull
        # the selected row into view.
        sel_row = self.selected // TILE_COLS
        if sel_row < self.scroll:
            self.scroll = sel_row
        elif sel_row >= self.scroll + TILE_ROWS_VIS:
            self.scroll = sel_row - TILE_ROWS_VIS + 1

    # ------------------------------------------------------------------
    # main tick — called by code.py at ~30 Hz
    # ------------------------------------------------------------------
    def tick(self):
        events = self.inputs.poll()
        events = self._consume_wake(events)
        if self.state == STATE_GRID:
            self._tick_grid(events)
        elif self.state == STATE_SETUP:
            self._tick_setup(events)
        # STATE_RUNNING progresses inline inside _run() and doesn't use tick().

    def _consume_wake(self, events):
        """Filter a wake-press (and anything held across the dim/wake edge)
        out of events, so tapping the display to light it up doesn't also
        select/abort/pass-through. Release the held key to re-enable."""
        now_ms = time.monotonic_ns() // 1_000_000
        if events:
            self._last_activity_ms = now_ms
        if self._dimmed:
            if events:
                self._set_brightness(FULL_BRIGHTNESS)
                self._dimmed = False
                self._wake_blocked = {
                    k for k, v in self.inputs.held.items() if v
                }
                return {}
        elif now_ms - self._last_activity_ms >= DIM_IDLE_MS:
            self._set_brightness(DIM_BRIGHTNESS)
            self._dimmed = True
        if self._wake_blocked:
            for k in list(self._wake_blocked):
                if not self.inputs.held.get(k):
                    self._wake_blocked.discard(k)
            if events:
                events = {k: v for k, v in events.items()
                          if k not in self._wake_blocked}
        return events

    def _set_brightness(self, b):
        if self.display is None:
            return
        try:
            self.display.brightness = b
        except Exception:
            pass

    def _btn_active(self, key):
        return self.inputs.held.get(key) and key not in self._wake_blocked

    def _masked_held(self, held):
        if not self._wake_blocked:
            return held
        return {k: (v and k not in self._wake_blocked) for k, v in held.items()}

    def _tick_grid(self, events):
        nav = _direction_event(events)
        if nav is not None and self.macros:
            n = len(self.macros)
            if nav == "LEFT":
                self.selected = max(0, self.selected - 1)
            elif nav == "RIGHT":
                self.selected = min(n - 1, self.selected + 1)
            elif nav == "UP":
                self.selected = max(0, self.selected - TILE_COLS)
            elif nav == "DOWN":
                self.selected = min(n - 1, self.selected + TILE_COLS)
            # Update scroll (don't re-read filesystem — _enter handles that
            # when we return to GRID from another state).
            sel_row = self.selected // TILE_COLS
            if sel_row < self.scroll:
                self.scroll = sel_row
            elif sel_row >= self.scroll + TILE_ROWS_VIS:
                self.scroll = sel_row - TILE_ROWS_VIS + 1
            self._render()
        elif "A" in events and self.macros:
            self._enter(STATE_SETUP)
        elif "CTRL" in events and self.macros:
            self._enter(STATE_SETUP)
        elif "Y" in events:
            self._reboot_to_usb_drive()

    def _tick_setup(self, events):
        held = self.inputs.held
        blocked = getattr(self, "_setup_blocked", None)
        if blocked:
            for k in list(blocked):
                if not held.get(k):
                    blocked.discard(k)
        hat = _hat_from_held(self._masked_held(held))
        buttons = 0
        if held["A"] and not (blocked and "A" in blocked) and "A" not in self._wake_blocked:
            buttons |= BTN["A"]
        if held["B"] and not (blocked and "B" in blocked) and "B" not in self._wake_blocked:
            buttons |= BTN["B"]
        if held["CTRL"] and not (blocked and "CTRL" in blocked) and "CTRL" not in self._wake_blocked:
            buttons |= BTN["X"]
        # Only transmit when something changed, to avoid flooding the host.
        state_key = (buttons, hat)
        if state_key != getattr(self, "_last_passthrough", None):
            self.pad.send_state(buttons=buttons, hat=hat)
            self._last_passthrough = state_key
        if "X" in events:
            self.pad.neutral()
            self._last_passthrough = None
            self._enter(STATE_GRID)
        elif "Y" in events:
            self.pad.neutral()
            self._last_passthrough = None
            self._run()

    def _reboot_to_usb_drive(self):
        """Set NVM flag + hard reset. boot.py reads the flag on the next
        boot, enables USB mass storage, and clears it — so the boot after
        that is back to normal HID-only mode. Equivalent to holding BTN_Y
        during boot, but software-triggered from the menu."""
        try:
            microcontroller.nvm[0] = NVM_USB_DRIVE_FLAG
        except Exception as e:
            print("nvm write failed:", repr(e))
            self.status_text = "nvm write failed"
            self._render()
            return
        self._render_rebooting()
        if self.display is not None:
            try:
                self.display.refresh()
            except Exception:
                pass
        time.sleep(0.5)
        microcontroller.reset()

    # ------------------------------------------------------------------
    # run — blocking, with progress+abort polled through progress_cb
    # ------------------------------------------------------------------
    def _run(self):
        self.state = STATE_RUNNING
        name = self.macros[self.selected]
        path = config.MACRO_DIR + "/" + name
        # Estimated total runtime from the .mz header (seconds). ETA in the
        # progress UI uses this minus the BRUSH_PREAMBLE seconds (measured on
        # device, ~2.1s) subtracted from wall-clock elapsed. None for .txt
        # macros (no header) → UI omits the ETA string.
        self._est_total_sec = _read_macro_estimate(path)

        # Show a loading screen during the ~2.5s BRUSH_PREAMBLE before the
        # first opcode fires. Without an explicit refresh here the display
        # can be stuck mid-transition between SETUP and RUNNING — auto_refresh
        # runs at 60Hz in a background task, so there's a window where we've
        # mutated the displayio tree but no refresh has fired yet, and we're
        # about to disable auto_refresh for the duration of the macro.
        self._render_preparing(name, path)
        display = self.display
        if display is not None:
            try:
                display.refresh()
            except Exception:
                pass

        # Poll inputs every call so B-abort lands (debouncer needs several
        # polls to register a press). Throttle the screen redraw to ~20Hz
        # so displayio doesn't thrash during dense macro sections.
        #
        # auto_refresh is disabled for the run so displayio can't steal the
        # SPI/USB tasks mid-HOLD and stretch our release edge past the next
        # 8ms Switch poll. We call display.refresh() explicitly from the
        # throttled draw path instead.
        next_draw_ms = [0]
        rendered_running = [False]
        # Cumulative pause time (seconds) subtracted from elapsed wall-clock
        # so the ETA and percent don't drift forward while paused.
        pause_accum_s = [0.0]

        def progress_cb(pct, i, total):
            events = self.inputs.poll()
            events = self._consume_wake(events)
            if "B" in events or self._btn_active("B"):
                raise MacroAbort()
            pause_ns = 0
            if "Y" in events:
                elapsed_at_pause = time.monotonic() - t0 - pause_accum_s[0]
                pause_ns = self._run_pause(elapsed_at_pause, i, total)
                pause_accum_s[0] += pause_ns / 1_000_000_000
            now_ms = time.monotonic_ns() // 1_000_000
            if now_ms >= next_draw_ms[0] or i == total:
                if not rendered_running[0]:
                    self._render_running()
                    rendered_running[0] = True
                elapsed = time.monotonic() - t0 - pause_accum_s[0]
                self._draw_progress(i, total, elapsed)
                if display is not None:
                    try:
                        display.refresh()
                    except Exception:
                        pass
                next_draw_ms[0] = now_ms + 50
            # Non-zero return shifts the macro_runner deadline scheduler
            # forward so post-resume events don't fire in a burst.
            return pause_ns

        # Cheap abort poller called from inside _wait_until every ~20ms so
        # B lands during long HOLDs. No display work — a refresh mid-HOLD
        # would blow the skew budget.
        def cancel_cb():
            events = self.inputs.poll()
            events = self._consume_wake(events)
            if "B" in events or self._btn_active("B"):
                raise MacroAbort()

        t0 = time.monotonic()
        if display is not None:
            display.auto_refresh = False
        try:
            macro_runner.run_macro(self.pad, path, progress_cb, cancel_cb)
            elapsed_s = time.monotonic() - t0
            # Post-completion notification. Runs here (not inside run_macro)
            # so the UI can swap the status text during the 2-5s wifi + POST
            # window. Only fires if the user actually set up secrets.py.
            try:
                import ntfy
                if ntfy.is_configured():
                    self._status.text = "Sending notification..."
                    if display is not None:
                        try:
                            display.refresh()
                        except Exception:
                            pass
                    stem = name
                    for ext in (".mz", ".txt"):
                        if stem.endswith(ext):
                            stem = stem[:-len(ext)]
                            break
                    ntfy.send("Print complete: " + stem,
                              title="Tomodachi Printer")
            except Exception as e:
                print("ntfy hook failed:", e)
            self.status_text = "Done: {} ({:.1f}s)".format(name, elapsed_s)
        except MacroAbort:
            self.pad.neutral()
            self.status_text = "Aborted: " + name
        except Exception as e:
            self.pad.neutral()
            msg = str(e) or repr(e) or type(e).__name__
            self.status_text = "Error: " + msg
            print("run_macro error:", repr(e))
            print("--- traceback attempt ---")
            try:
                import traceback
                traceback.print_exception(e)
                print("(via traceback)")
            except Exception as te:
                print("traceback failed:", repr(te))
            try:
                import sys
                sys.print_exception(e)
                print("(via sys)")
            except Exception as te:
                print("sys.print_exception failed:", repr(te))
            print("--- end traceback ---")
        finally:
            if display is not None:
                display.auto_refresh = True
        self._enter(STATE_GRID)

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _render(self):
        if self.display is None:
            self._render_console()
            return
        if self.state == STATE_GRID:
            self._render_grid()
        elif self.state == STATE_SETUP:
            self._render_setup()
        elif self.state == STATE_RUNNING:
            self._render_running()

    def _render_console(self):
        if self.state == STATE_GRID:
            print("[GRID]", self.selected, "/", len(self.macros), self.status_text)
        elif self.state == STATE_SETUP:
            print("[SETUP]")
        elif self.state == STATE_RUNNING:
            print("[RUNNING]", self.status_text)

    def _set_buttons(self, a, b, x, y):
        labels = (a, b, x, y)
        for i, t in enumerate(labels):
            self._btns[i].text = t

    def _render_grid(self):
        self._title.text = "Tomodachi Print"
        self._title.color = COLOR_ACCENT

        # Hide list-mode elements; GRID uses the thumb_group.
        self._hide_highlight()
        self._hide_bar()
        self._hide_setup_preview()
        for lbl in self._rows:
            lbl.text = ""

        if not self.macros:
            # Empty-state: clear every slot and hide the selection border.
            for i, slot in enumerate(self._thumb_slots):
                if self._thumb_last[i] is not None:
                    while len(slot) > 0:
                        slot.pop()
                    self._thumb_last[i] = None
            self._grid_hl.y = -200
            self._status.text = self.status_text or "No macros. Press Y to import."
            self._status.color = COLOR_DIM
            self._set_buttons("", "", "", "IMPORT")
            return

        first = self.scroll * TILE_COLS
        sel_tile_xy = None
        n_slots = TILE_COLS * TILE_ROWS_VIS

        for i in range(n_slots):
            macro_idx = first + i
            target = self.macros[macro_idx] if macro_idx < len(self.macros) else None
            r = i // TILE_COLS
            c = i % TILE_COLS
            tile_x = TILE_X0 + c * (TILE_SIZE + TILE_GAP_X)
            tile_y = TILE_Y0 + r * TILE_ROW_PITCH

            if macro_idx == self.selected:
                sel_tile_xy = (tile_x, tile_y)

            # Only rebuild the slot when its macro actually changed. Selection
            # movement within the visible window is a pure highlight reposition
            # — no OnDiskBitmap reload, no label re-creation.
            if self._thumb_last[i] != target:
                slot = self._thumb_slots[i]
                while len(slot) > 0:
                    slot.pop()
                if target is not None:
                    self._populate_slot(slot, target, tile_x, tile_y)
                self._thumb_last[i] = target

        if sel_tile_xy is not None:
            self._grid_hl.x = sel_tile_xy[0] - TILE_HL_INSET
            self._grid_hl.y = sel_tile_xy[1] - TILE_HL_INSET
        else:
            self._grid_hl.y = -200

        self._status.text = self.status_text or "{} / {}".format(
            self.selected + 1, len(self.macros),
        )
        self._status.color = COLOR_DIM
        self._set_buttons("run", "", "", "IMPORT")

    def _populate_slot(self, slot, macro_name, tile_x, tile_y):
        """Build the tile + name label for macro_name into an empty slot
        Group. Uses an OnDiskBitmap tile if <stem>_macro.bmp exists,
        otherwise a dim-gray '?' placeholder."""
        thumb_path = self._thumb_path_for(macro_name)
        tile_added = False
        if thumb_path is not None:
            try:
                bmp = displayio.OnDiskBitmap(thumb_path)
                slot.append(displayio.TileGrid(
                    bmp, pixel_shader=bmp.pixel_shader, x=tile_x, y=tile_y,
                ))
                tile_added = True
            except Exception:
                tile_added = False
        if not tile_added:
            bg_bmp = displayio.Bitmap(TILE_SIZE, TILE_SIZE, 1)
            bg_pal = displayio.Palette(1)
            bg_pal[0] = 0x202020
            slot.append(displayio.TileGrid(
                bg_bmp, pixel_shader=bg_pal, x=tile_x, y=tile_y,
            ))
            slot.append(label.Label(
                terminalio.FONT, text="?", color=COLOR_DIM, scale=3,
                anchor_point=(0.5, 0.5),
                anchored_position=(
                    tile_x + TILE_SIZE // 2, tile_y + TILE_SIZE // 2,
                ),
            ))

        display_name = macro_name
        if display_name.endswith(".mz"):
            display_name = display_name[:-3]
        elif display_name.endswith(".txt"):
            display_name = display_name[:-4]
        if display_name.endswith("_macro"):
            display_name = display_name[:-6]
        if len(display_name) > 11:
            display_name = display_name[:10] + "…"
        slot.append(label.Label(
            terminalio.FONT, text=display_name, color=COLOR_FG,
            anchor_point=(0.5, 0.0),
            anchored_position=(
                tile_x + TILE_SIZE // 2, tile_y + TILE_SIZE + 3,
            ),
        ))

    def _thumb_path_for(self, macro_name):
        """Map 'chopper_macro.mz' → '/macros/chopper_macro.bmp'. Returns None
        if no such file exists (so callers render the placeholder)."""
        if macro_name.endswith(".mz"):
            base = macro_name[:-3]
        elif macro_name.endswith(".txt"):
            return None  # .txt macros are test macros; no thumbnails.
        else:
            base = macro_name
        path = config.MACRO_DIR + "/" + base + ".bmp"
        try:
            import os as _os
            _os.stat(path)
            return path
        except OSError:
            return None

    def _hide_thumbs(self):
        if self.display is None:
            return
        # Clear each slot's contents but keep the slot sub-groups themselves
        # so _render_grid can repopulate them incrementally. Also invalidates
        # the cache so returning to GRID rebuilds.
        for i, slot in enumerate(self._thumb_slots):
            while len(slot) > 0:
                slot.pop()
            self._thumb_last[i] = None
        self._grid_hl.y = -200

    def _render_setup(self):
        self._hide_thumbs()
        self._title.text = "Prepare to Print"
        self._title.color = COLOR_WARN
        name = self.macros[self.selected] if self.macros else ""
        display_name = name
        if display_name.endswith(".mz"):
            display_name = display_name[:-3]
        elif display_name.endswith(".txt"):
            display_name = display_name[:-4]
        if display_name.endswith("_macro"):
            display_name = display_name[:-6]
        est = _read_macro_estimate(config.MACRO_DIR + "/" + name) if name else None
        if est is None:
            est_str = "Est. time: unknown"
        elif est >= 60:
            est_str = "Est. time: {}m{:02d}s".format(int(est) // 60, int(est) % 60)
        else:
            est_str = "Est. time: {}s".format(int(est))
        lines = (
            "Joystick = D-pad",
            "Joystick click = X",
            "",
            "Selected: " + display_name,
            est_str,
        )
        for i, lbl in enumerate(self._rows):
            lbl.text = lines[i] if i < len(lines) else ""
            lbl.color = COLOR_FG
        # Thumbnail below the "Selected:" line, centered in the left area.
        preview_x = (config.LCD_WIDTH - 60 - TILE_SIZE) // 2
        preview_y = LIST_Y0 + 5 * LIST_ROW_H + 16
        if self._setup_preview_last != name:
            while len(self._setup_preview) > 0:
                self._setup_preview.pop()
            self._populate_preview(name, preview_x, preview_y)
            self._setup_preview_last = name
        self._hide_highlight()
        self._hide_bar()
        self._status.text = "passthrough HID active"
        self._status.color = COLOR_WARN
        self._set_buttons("A", "B", "back", "START")

    def _populate_preview(self, macro_name, x, y):
        thumb_path = self._thumb_path_for(macro_name)
        if thumb_path is not None:
            try:
                bmp = displayio.OnDiskBitmap(thumb_path)
                self._setup_preview.append(displayio.TileGrid(
                    bmp, pixel_shader=bmp.pixel_shader, x=x, y=y,
                ))
                return
            except Exception:
                pass
        bg_bmp = displayio.Bitmap(TILE_SIZE, TILE_SIZE, 1)
        bg_pal = displayio.Palette(1)
        bg_pal[0] = 0x202020
        self._setup_preview.append(displayio.TileGrid(
            bg_bmp, pixel_shader=bg_pal, x=x, y=y,
        ))
        self._setup_preview.append(label.Label(
            terminalio.FONT, text="?", color=COLOR_DIM, scale=3,
            anchor_point=(0.5, 0.5),
            anchored_position=(x + TILE_SIZE // 2, y + TILE_SIZE // 2),
        ))

    def _hide_setup_preview(self):
        if self.display is None:
            return
        while len(self._setup_preview) > 0:
            self._setup_preview.pop()
        self._setup_preview_last = None

    def _render_running(self):
        self._hide_thumbs()
        self._title.text = "PRINTING"
        self._title.color = COLOR_ACCENT
        name = self.macros[self.selected] if self.macros else ""
        for i, lbl in enumerate(self._rows):
            lbl.text = ""
        self._rows[0].text = name
        self._rows[0].color = COLOR_FG
        self._rows[2].text = "0.00%"
        self._rows[2].color = COLOR_ACCENT
        self._rows[7].text = "0 / 0"
        self._rows[7].color = COLOR_DIM
        # Thumbnail in the bottom-right corner — reuses the setup preview
        # group since SETUP and RUNNING never coexist.
        preview_x = config.LCD_WIDTH - 60 - TILE_SIZE - 2
        preview_y = config.LCD_HEIGHT - TILE_SIZE - 24
        while len(self._setup_preview) > 0:
            self._setup_preview.pop()
        self._populate_preview(name, preview_x, preview_y)
        self._setup_preview_last = None
        self._hide_highlight()
        self._show_bar(0.0)
        est = getattr(self, "_est_total_sec", None)
        self._status.text = _format_eta(est, 0.0) if est is not None else ""
        self._status.color = COLOR_WARN
        self._set_buttons("", "STOP", "", "pause")

    def _render_paused(self):
        if self.display is None:
            print("[PAUSED]")
            return
        self._title.text = "PAUSED (HID passthrough)"
        self._title.color = COLOR_WARN
        for i, lbl in enumerate(self._rows):
            lbl.text = ""
        self._rows[0].text = "Joystick = D-pad"
        self._rows[0].color = COLOR_FG
        self._rows[1].text = "Joystick click = X"
        self._rows[1].color = COLOR_FG
        self._rows[3].text = "A/B passthrough live"
        self._rows[3].color = COLOR_DIM
        self._rows[5].text = "Don't move the cursor —"
        self._rows[5].color = COLOR_ERR
        self._rows[6].text = "macro will misalign."
        self._rows[6].color = COLOR_ERR
        self._hide_highlight()
        self._hide_bar()
        self._status.text = "Y=resume  X=screenshot"
        self._status.color = COLOR_WARN
        self._set_buttons("A", "B", "screenshot", "resume")

    def _run_pause(self, elapsed_at_pause=0.0, last_i=0, last_total=0):
        """Blocking pause loop: HID passthrough from Pico inputs to Switch,
        while the macro's absolute-deadline scheduler is frozen. Returns the
        paused duration in nanoseconds so the caller can shift t_cursor
        forward by that amount — otherwise post-resume events would fire
        back-to-back until they caught up to wall clock. GC stays enabled
        while paused (main run disables it); re-disabled on resume.

        Safety posture: this loop prioritizes never losing a Pico input
        press over latency. Polling runs every ~5ms so a human-length press
        (50ms+) always spans many debouncer samples. We never return early
        on partial state (e.g., if Y and X are both held, we still finish
        the current iteration so passthrough stays coherent). Screenshot
        (X press) is handled inline so the macro's pad state is guaranteed
        to be at NEUTRAL before entering pause (progress_cb is called at
        opcode boundaries, which always end in a NEUTRAL write) — firing
        CAPTURE + release here can't race any other HID writer.
        """
        pause_start_ns = time.monotonic_ns()
        self.pad.neutral()
        gc.enable()
        gc.collect()
        self._render_paused()
        if self.display is not None:
            try:
                self.display.refresh()
            except Exception:
                pass
        # Lock out any mapped button held at pause entry so it isn't forwarded
        # through to the Switch (e.g. A still asserted from a just-completed
        # STAMP won't re-confirm anything). Each bit clears on release.
        blocked = {
            k for k in ("A", "B", "CTRL") if self.inputs.held.get(k)
        }
        last_state = None
        while True:
            events = self.inputs.poll()
            events = self._consume_wake(events)
            held = self.inputs.held
            if blocked:
                for k in list(blocked):
                    if not held.get(k):
                        blocked.discard(k)
            hat = _hat_from_held(self._masked_held(held))
            buttons = 0
            if held["A"] and "A" not in blocked and "A" not in self._wake_blocked:
                buttons |= BTN["A"]
            if held["B"] and "B" not in blocked and "B" not in self._wake_blocked:
                buttons |= BTN["B"]
            if held["CTRL"] and "CTRL" not in blocked and "CTRL" not in self._wake_blocked:
                buttons |= BTN["X"]
            if "X" in events:
                # Fire CAPTURE on top of whatever the user is currently
                # holding so the passthrough state stays coherent — then
                # drop CAPTURE while keeping the rest asserted. Reset
                # last_state so the top-of-loop diff re-sends the current
                # held state even if it happens to match what we just sent.
                try:
                    self.pad.send_state(
                        buttons=buttons | BTN["CAPTURE"], hat=hat,
                    )
                    time.sleep(0.1)
                    self.pad.send_state(buttons=buttons, hat=hat)
                    time.sleep(0.03)
                except Exception as e:
                    print("screenshot failed:", repr(e))
                    self.pad.neutral()
                last_state = None
                continue
            state_key = (buttons, hat)
            if state_key != last_state:
                self.pad.send_state(buttons=buttons, hat=hat)
                last_state = state_key
            if "Y" in events:
                break
            time.sleep(0.005)
        self.pad.neutral()
        self._render_running()
        # _render_running defaults status+bar to "0.0 elapsed" — if we just
        # refreshed that, the display would flash "full ETA / 0%" for ~50ms
        # until the next progress_cb draw tick. Overwrite with the elapsed
        # captured at pause entry (the counter is frozen here, so that's the
        # correct resume value) before refreshing.
        if last_total:
            self._draw_progress(last_i, last_total, elapsed_at_pause)
        if self.display is not None:
            try:
                self.display.refresh()
            except Exception:
                pass
        gc.collect()
        gc.disable()
        return time.monotonic_ns() - pause_start_ns

    def _render_preparing(self, name, path):
        brush = _read_macro_brush(path)
        if self.display is None:
            print("[PREPARING]", name, "brush=", brush)
            return
        self._hide_thumbs()
        self._hide_setup_preview()
        self._title.text = "PREPARING"
        self._title.color = COLOR_WARN
        for i, lbl in enumerate(self._rows):
            lbl.text = ""
        self._rows[0].text = name
        self._rows[0].color = COLOR_FG
        self._rows[2].text = "Setting %s brush..." % brush
        self._rows[2].color = COLOR_DIM
        self._hide_highlight()
        self._hide_bar()
        self._status.text = ""
        self._set_buttons("", "", "", "")

    def _draw_progress(self, i, total, elapsed=0.0):
        if self.display is None:
            if total and (i * 100 // total) % 5 == 0:
                print("progress", i, "/", total)
            return
        # Percent + ETA both derive from wall-clock elapsed vs header estimate
        # so they stay in lockstep. Byte progress (i/total) drifts relative to
        # time because REPEAT opcodes are 2 bytes but span seconds, and PAUSE
        # opcodes are 3 bytes for any duration — so a byte-based % appears to
        # race ahead of the countdown even though neither is "wrong".
        est = getattr(self, "_est_total_sec", None)
        done = (i >= total) if total else False
        if est is not None and est > 0:
            frac = 1.0 if done else min(1.0, elapsed / est)
            self._status.text = _format_eta(est, elapsed)
        else:
            frac = (i / total) if total else 1.0
        self._rows[2].text = "{:.2f}%".format(frac * 100.0)
        self._rows[7].text = "{} / {}".format(i, total)
        self._show_bar(frac)

    def _show_bar(self, frac):
        if self.display is None:
            return
        if frac < 0.0:
            frac = 0.0
        elif frac > 1.0:
            frac = 1.0
        self._bar_fill.y = BAR_Y
        self._bar_cover.y = BAR_Y
        # Cover slides right: at 0% it fully covers fill; at 100% it sits
        # entirely off the right edge of the bar.
        self._bar_cover.x = BAR_X + int(round(frac * BAR_W))

    def _hide_bar(self):
        if self.display is None:
            return
        self._bar_fill.y = BAR_HIDDEN_Y
        self._bar_cover.y = BAR_HIDDEN_Y

    def _render_rebooting(self):
        if self.display is None:
            print("[REBOOTING -> USB DRIVE]")
            return
        self._hide_thumbs()
        self._hide_setup_preview()
        self._title.text = "Rebooting..."
        self._title.color = COLOR_WARN
        for i, lbl in enumerate(self._rows):
            lbl.text = ""
        self._rows[0].text = "USB drive mode"
        self._rows[0].color = COLOR_FG
        self._rows[2].text = "CIRCUITPY will mount"
        self._rows[2].color = COLOR_DIM
        self._rows[3].text = "on your computer."
        self._rows[3].color = COLOR_DIM
        self._rows[5].text = "Reboot again to"
        self._rows[5].color = COLOR_DIM
        self._rows[6].text = "return to HID mode."
        self._rows[6].color = COLOR_DIM
        self._hide_highlight()
        self._hide_bar()
        self._status.text = ""
        self._set_buttons("", "", "", "")

    def _position_highlight(self, y, width, height):
        self._hl.y = y

    def _hide_highlight(self):
        self._hl.y = -100


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _format_eta(est_total_sec, elapsed):
    rem_sec = int(est_total_sec - elapsed) if est_total_sec > elapsed else 0
    if rem_sec >= 60:
        return "~{}m{:02d}s remaining".format(rem_sec // 60, rem_sec % 60)
    return "~{}s remaining".format(rem_sec)


def _read_macro_estimate(path):
    """Return estimated runtime (seconds) from a .mz file's 8-byte header,
    or None for headerless files (.txt, bare .mz). Header layout:
    'MZ1' + version byte + uint32 BE ms."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(8)
    except OSError:
        return None
    if (len(hdr) >= 8 and hdr[0] == 0x4D and hdr[1] == 0x5A
            and hdr[2] == 0x31):
        ms = (hdr[4] << 24) | (hdr[5] << 16) | (hdr[6] << 8) | hdr[7]
        return ms / 1000.0
    return None


_BRUSH_BY_VERSION = {0x01: "1x1", 0x02: "3x3", 0x03: "plus"}


def _read_macro_brush(path):
    """Return the preamble's target brush ('1x1', '3x3', 'plus') based on the
    .mz header version byte. Defaults to '1x1' for headerless/missing files."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(4)
    except OSError:
        return "1x1"
    if (len(hdr) >= 4 and hdr[0] == 0x4D and hdr[1] == 0x5A
            and hdr[2] == 0x31):
        return _BRUSH_BY_VERSION.get(hdr[3], "1x1")
    return "1x1"


def _hat_from_held(held):
    u = held["UP"]; d = held["DOWN"]; l = held["LEFT"]; r = held["RIGHT"]
    if u and r: return HAT["DPAD_UP_RIGHT"]
    if d and r: return HAT["DPAD_DOWN_RIGHT"]
    if d and l: return HAT["DPAD_DOWN_LEFT"]
    if u and l: return HAT["DPAD_UP_LEFT"]
    if u: return HAT["DPAD_UP"]
    if d: return HAT["DPAD_DOWN"]
    if l: return HAT["DPAD_LEFT"]
    if r: return HAT["DPAD_RIGHT"]
    return HAT_NEUTRAL


def _direction_event(events):
    """Collapse the joystick events into a single 'UP'/'DOWN'/'LEFT'/'RIGHT'
    string, honoring both 'press' and 'repeat' so a held joystick autoscrolls."""
    for d in ("UP", "DOWN", "LEFT", "RIGHT"):
        if events.get(d) in ("press", "repeat"):
            return d
    return None
