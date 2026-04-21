# Printing The Dream

A tool for *Tomodachi Life: Living the Dream* to import images. Zero modification to the game console is required, works on both Switch 1 and Switch 2.

A microcontroller draws images in the Palette House by emulating a wired controller over USB. Runs on a Raspberry Pi Pico 2 W with CircuitPython.

https://github.com/user-attachments/assets/552b5734-b5ef-4eca-8a5f-6ab6c6145830

## How it works

```
PNG  ── convert.py ──>  .mz macro  ──>  Pico emulates HORIPAD ──>  Image is drawn
```

`convert.py` quantizes a 256x256px input image to the in-game palette, orders pixels with a color-grouped TSP path, picks the fastest brush combination, and writes a compact binary macro. The Pico runs that macro against the Switch via USB HID, one tap at a time.

## Hardware

- Raspberry Pi Pico 2 WH (RP2350 + WiFi) — *Make sure to get the "WH" variant unless you know how to solder.*
- Waveshare Pico-LCD-1.3 (240×240 ST7789 + joystick + 4 buttons) — plugs directly onto the Pico's headers. The pinout in `pico/config.py` assumes this exact module; other displays will need pin edits.
- Micro-USB cable to connect the Pico to your Switch (docked or undocked).

At my time of purchase, the parts came to just under $30.

## Getting started

- **[docs/quickstart.md](docs/quickstart.md)** — concise build + run instructions. Assumes technical knowledge.

## Project layout

```
convert.py              PNG → macro pipeline (runs on your computer)
reference/palette.json  in-game palette + grid coordinates
pico/                   CircuitPython firmware (copy to CIRCUITPY root)
  boot.py               registers HORIPAD-S HID descriptor, toggles MSC
  code.py               main entry; wires display + inputs + UI
  config.py             pinout, USB HID identity
  display.py            ST7789 init
  inputs.py             debounced joystick + button poll
  horipad_hid.py        HORIPAD S 8-byte HID report assembly
  macro_runner.py       macro decoder + HID scheduler
  ui.py                 GRID / SETUP / RUNNING state machine
  ntfy.py               optional ntfy.sh push on print completion
  secrets_example.py    template for wifi + ntfy config (rename to secrets.py)
  macros/               place .mz macros here
docs/                   quickstart guide
```
