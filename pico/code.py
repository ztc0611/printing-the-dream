"""code.py — CircuitPython main entry.

Wiring:
  boot.py         registers HORIPAD-S HID + decides MSC mode
  display.py      initializes the ST7789 and returns the Display
  inputs.py       debounced poll of joystick + 4 buttons
  ui.py           state machine (GRID / SETUP / RUNNING / DONE)
  macro_runner.py executes a macro against the HID, called from UI
"""
import os
import time

import storage

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


def _file_import_splash(disp):
    """Static splash shown when boot.py left MSC enabled (held Y, nvm flag,
    or no macros on disk). The UI loop never runs in this state — host owns
    the filesystem, the only useful user action is drag-drop and reboot."""
    import displayio
    import terminalio
    from adafruit_display_text import label

    root = displayio.Group()
    bg_bmp = displayio.Bitmap(config.LCD_WIDTH, config.LCD_HEIGHT, 1)
    bg_pal = displayio.Palette(1)
    bg_pal[0] = 0x000000
    root.append(displayio.TileGrid(bg_bmp, pixel_shader=bg_pal))

    root.append(label.Label(
        terminalio.FONT, text="File Import Mode",
        color=0xFFCC00, x=6, y=10,
    ))
    body = (
        ("Connect to a computer to",  0xFFFFFF),
        ("add macros.",               0xFFFFFF),
        ("",                          0x000000),
        ("Drop .mz files into the",   0x808080),
        ("macros/ folder on the",     0x808080),
        ("CIRCUITPY drive.",          0x808080),
        ("",                          0x000000),
        ("Reboot without holding Y",  0x808080),
        ("to return to print mode.",  0x808080),
    )
    y = 40
    for text, color in body:
        if text:
            root.append(label.Label(
                terminalio.FONT, text=text, color=color, x=6, y=y,
            ))
        y += 12
    disp.root_group = root


def main():
    pad = HoriPad()
    pad.neutral()

    disp = display.init()

    # When boot.py enabled MSC the filesystem is host-owned and readonly
    # from our side. Don't run the macro UI — show a dedicated File Import
    # splash and idle until the user reboots.
    if storage.getmount("/").readonly:
        _file_import_splash(disp)
        while True:
            time.sleep(1)

    inputs = Inputs()
    app = ui.UI(disp, inputs, pad, _list_macros)

    print("UI up. Macros:", _list_macros())
    while True:
        app.tick()
        time.sleep(0.03)


main()
