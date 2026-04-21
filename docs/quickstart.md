# Quickstart

Concise build and run guide. Assumes you've flashed a dev board before, know how to install Python packages, and can follow a pinout.

## Flash the Pico

1. Grab the latest **CircuitPython 9+ for Raspberry Pi Pico 2 W** from <https://circuitpython.org/board/raspberry_pi_pico2_w/>.
2. Hold BOOTSEL while plugging in the Pico, drag the `.uf2` onto the `RPI-RP2` drive that appears. The Pico reboots as a `CIRCUITPY` drive.
3. Install the two required libraries into `CIRCUITPY/lib/`:
   - `adafruit_st7789`
   - `adafruit_display_text`
   Both live in the official CircuitPython library bundle (<https://circuitpython.org/libraries>) — download the bundle that matches your CircuitPython version, then copy those two folders into `CIRCUITPY/lib/`.
4. Copy the contents of `pico/` from this repo into the root of `CIRCUITPY`: `boot.py`, `code.py`, `config.py`, `display.py`, `horipad_hid.py`, `inputs.py`, `macro_runner.py`, `ntfy.py`, `ui.py`, and the empty `macros/` directory. (`secrets_example.py` is optional, see below.)
5. Eject and replug. The display should light up showing an empty macro list.

**Optional — push notification on print completion.** When the Pico finishes a print it tries to POST a message to [ntfy.sh](https://ntfy.sh/). Off by default; the printer runs identically without it. To enable:

1. Copy `pico/secrets_example.py` from this repo to `CIRCUITPY/secrets.py` (rename on copy).
2. Fill in your wifi creds and an ntfy topic.
3. Drop `adafruit_requests` from the CircuitPython bundle into `CIRCUITPY/lib/`.

After the first boot the Pico disables its USB mass-storage drive (so the Switch sees only the HID gamepad). To remount the drive for editing, hold button **Y** while plugging in, or press **Y** from the macro-list screen. The UI reboots the Pico with MSC re-enabled for one boot.

## Generate a macro

```
python3 convert.py path/to/image.png
```

Defaults to 256×256 (the in-game canvas size). Outputs land in `out/<name>/`:

- `<name>_macro.mz` — the binary macro to load onto the Pico
- `<name>_macro.bmp` — the quantized thumbnail the UI shows
- `<name>_preview.png` — full-res palette-snap preview so you can see what you're going to get (don't copy to the Pico)

Requirements: Python 3.10+, `numpy`, `Pillow`.

Useful flags:

- `--brushes auto` (default) — dry-runs every brush combo in parallel and picks the fastest. Pass `--brushes 1x1` to force single-pixel only.
- `--quant oklab` — perceptually uniform color matching. Slower and usually longer runtime, but better hue fidelity on saturated gradients.
- `--saturation 1.3` — boost saturation before quantizing. Helps when the source has muted colors that snap to greys.
- `--debug` — writes the following for debugging into `out/<name>/`:
   - `<name>_simulated.png` — dry-run trace of what the macro will actually draw
   - `<name>_macro_info.json` — stats (stamp count, estimated runtime, etc.)

## Run a print

1. Copy `<name>_macro.mz` and the matching `.bmp` thumbnail into `CIRCUITPY/macros/`.
2. Open Palette House, get to the point right after the message about being allowed to use the touch screen. DO NOT press any buttons at this point.
3. Plug the Pico into the Switch.
4. On the Pico's display, pick your macro with the joystick + **A**.
5. On the **SETUP** screen the Pico's joystick and buttons act as a direct HID passthrough. Use them to make the Switch accept this controller. Press A to bring up the "Press L+R on the controller." menu, then hit A until it dismisses and you are on the canvas. When ready, press **Y** to start.
6. The **RUNNING** screen shows a progress bar. Press **B** to abort.

A 256×256 image typically takes 40–80 minutes depending on color count and complexity. Runtime is printed by `convert.py` after generation and displayed on the screen while printing.