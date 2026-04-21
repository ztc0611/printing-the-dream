"""code.py — CircuitPython main entry.

Wiring:
  boot.py         registers HORIPAD-S HID + disables USB drive
  display.py      initializes the ST7789 and returns the Display
  inputs.py       debounced poll of joystick + 4 buttons
  ui.py           state machine (GRID / SETUP / RUNNING)
  macro_runner.py executes a macro against the HID, called from UI
"""
import os
import time

import config
import display
import ui
from horipad_hid import HoriPad
from inputs import Inputs


def _ensure_macro_dir():
    try:
        os.stat(config.MACRO_DIR)
    except OSError:
        os.mkdir(config.MACRO_DIR)


def _list_macros():
    _ensure_macro_dir()
    return sorted(f for f in os.listdir(config.MACRO_DIR)
                  if f.endswith(".txt") or f.endswith(".mz"))


def main():
    pad = HoriPad()
    pad.neutral()

    disp = display.init()
    inputs = Inputs()

    app = ui.UI(disp, inputs, pad, _list_macros)

    print("UI up. Macros:", _list_macros())
    while True:
        app.tick()
        time.sleep(0.03)


main()
