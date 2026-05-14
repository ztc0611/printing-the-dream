"""Debounced polling for the Waveshare Pico-LCD-1.3 joystick + 4 buttons.

All pins are active-LOW with internal pull-ups. `Inputs.poll()` returns a dict
of edge events since the last poll — keys are button names, values are 'press'
on the initial edge or 'repeat' at REPEAT_MS cadence after HOLD_MS. Releases
don't fire an event; callers read .held[name] for current state.
"""
import time

import board
import digitalio

import config


HOLD_MS = 350
REPEAT_MS = 120
DEBOUNCE_MS = 15


_PIN_ATTRS = (
    ("UP",    "JOY_UP"),
    ("DOWN",  "JOY_DOWN"),
    ("LEFT",  "JOY_LEFT"),
    ("RIGHT", "JOY_RIGHT"),
    ("CTRL",  "JOY_CTRL"),
    ("A",     "BTN_A"),
    ("B",     "BTN_B"),
    ("X",     "BTN_X"),
    ("Y",     "BTN_Y"),
)


def _pin(name):
    return getattr(board, name)


class Inputs:
    def __init__(self):
        self._pins = {}
        self.held = {}
        self._press_start = {}
        self._last_repeat = {}
        self._last_raw = {}
        self._last_change = {}
        for key, attr in _PIN_ATTRS:
            pin_name = getattr(config, attr)
            io = digitalio.DigitalInOut(_pin(pin_name))
            io.direction = digitalio.Direction.INPUT
            io.pull = digitalio.Pull.UP
            self._pins[key] = io
            self.held[key] = False
            self._press_start[key] = 0
            self._last_repeat[key] = 0
            self._last_raw[key] = False
            self._last_change[key] = 0

    def poll(self):
        now = time.monotonic_ns() // 1_000_000
        events = {}
        for key, io in self._pins.items():
            raw = not io.value  # active-low
            if raw != self._last_raw[key]:
                self._last_raw[key] = raw
                self._last_change[key] = now
                continue
            if now - self._last_change[key] < DEBOUNCE_MS:
                continue
            if raw and not self.held[key]:
                self.held[key] = True
                self._press_start[key] = now
                self._last_repeat[key] = now
                events[key] = "press"
            elif raw and self.held[key]:
                dt = now - self._press_start[key]
                if dt >= HOLD_MS and now - self._last_repeat[key] >= REPEAT_MS:
                    self._last_repeat[key] = now
                    events[key] = "repeat"
            elif not raw and self.held[key]:
                self.held[key] = False
        return events
