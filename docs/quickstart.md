# Quickstart

Concise build and run guide. Assumes you've flashed a dev board before, know how to install Python packages, and can follow a pinout.

## Flash the Pico

1. Grab the latest **CircuitPython 9+ for Raspberry Pi Pico 2 W** from <https://circuitpython.org/board/raspberry_pi_pico2_w/>.
2. Hold BOOTSEL while plugging in the Pico, drag the `.uf2` onto the `RPI-RP2` drive that appears. The Pico reboots as a `CIRCUITPY` drive.
3. Install the two required libraries into `CIRCUITPY/lib/`:
   - `adafruit_st7789`
   - `adafruit_display_text`
   Both live in the official CircuitPython library bundle (<https://circuitpython.org/libraries>) — download the bundle that matches your CircuitPython version, then copy those two folders into `CIRCUITPY/lib/`.
4. Copy the contents of `pico/` from this repo into `CIRCUITPY`, **preserving the directory structure**: `boot.py`, `code.py`, and the empty `macros/` directory go at the root; everything under `pico/lib/` merges into `CIRCUITPY/lib/` alongside the adafruit libraries you installed in step 3. (`secrets_example.txt` is optional, see below.)
5. Eject and replug. The display lights up showing the File Import splash (no macros yet).

**Optional — push notification on print completion.** When the Pico finishes a print it tries to POST a message to [ntfy.sh](https://ntfy.sh/). Off by default; the printer runs identically without it. To enable:

1. Copy `pico/secrets_example.txt` from this repo to `CIRCUITPY/secrets.txt` (rename on copy).
2. Open it in a text editor and fill in your wifi creds and an ntfy topic — it's a plain `KEY=VALUE` file, one per line.
3. Drop `adafruit_requests` from the CircuitPython bundle into `CIRCUITPY/lib/`.

If `macros/` has any `.mz` files in it, the Pico boots into HID-only mode and the `CIRCUITPY` drive is hidden so the Switch sees only the gamepad. If `macros/` is empty, or you hold button **Y** while plugging in, or you press **IMPORT** (Y) from the macro grid, the drive stays mounted so you can drag-drop more macros. Reboot without holding Y to return to print mode.

## Generate a macro

```
python3 convert.py path/to/image.png
```

Defaults to 256×256 (the food canvas size). Other item types have different rectangular canvases. Size your source image to match if you want to fill the item, or `convert.py` will letterbox with transparent padding (no stamps emitted for empty cells):

| Item | Canvas |
|---|---|
| Food | 256×256 |
| Book | 180×256 |
| Video | 256×131 |
| Game | 256×144 |

Video is 131px tall (odd). When vertical margins are uneven the extra pixel goes on the **top**. For example with a video, 63px from the top, 62px from the bottom.

Items with non-rectangular masks aren't listed. You'll need to shape your source image to match their silhouette yourself.

Outputs land in `out/<name>/`:

- `<name>.mz` — the binary macro to load onto the Pico. The Pico-side thumbnail is embedded inside this file; no separate bitmap is written.
- `<name>_preview.png` — full-res palette-snap preview so you can see what you're going to get (don't copy to the Pico)

Requirements: Python 3.10+, `numpy`, `Pillow`, `scipy`.

Useful flags:

- `--brushes 7x7,3x3,plus,1x1` (default) — brushes available to the per-color set-cover tiler, largest first. The tiler picks the cheapest subset per color group automatically. Pass `--brushes 1x1` to force single-pixel only.
- `--quant auto` (default) — generates the image with both RGB and OKLAB palette-snap in parallel and picks OKLAB when its runtime overhead is within either the percent or absolute-minute threshold (defaults 10% and 3 min). Pass `--quant rgb` or `--quant oklab` to force one. Tune the thresholds with `--oklab-threshold-pct` and `--oklab-threshold-min`.
- `--no-bucket` — disable paint-bucket scheduling. Use this when you want to overdraw on top of an existing canvas (see "Run a print" below).
- `--saturation 1.3` — boost saturation before quantizing. Helps when the source has muted colors that snap to greys.
- `--debug` — writes the following for debugging into `out/<name>/`:
   - `<name>_simulated.png` — dry-run trace of what the macro will actually draw
   - `<name>.json` — stats (stamp count, estimated runtime, etc.)

## Run a print

1. Copy `<name>.mz` into `CIRCUITPY/macros/` (the thumbnail is embedded inside the `.mz`).
2. Open Palette House, get to the point right after the message about being allowed to use the touch screen. DO NOT press any buttons at this point.
3. Plug the Pico into the Switch.
4. On the Pico's display, pick your macro with the joystick + **A**.
5. On the **SETUP** screen the Pico's joystick and buttons act as a direct HID passthrough. Use them to make the Switch accept this controller. Press A to bring up the "Press L+R on the controller." menu, then hit A until it dismisses and you are on the canvas. Confirm that the cursor is parked at canvas center and the palette is on pure black (bottom-left swatch). The SETUP screen displays the macro's estimated runtime and, if the macro uses paint-bucket fills, a "Uses paint bucket" line. When ready, press **PRINT** (the joystick click) to start.
6. The **RUNNING** screen shows a progress bar. Press **B** to abort.
7. When the macro completes, the **DONE** screen shows the thumbnail, elapsed time, and an in-band ntfy status if notifications are configured. Press **A** to return to the macro grid.

### Canvas prerequisites

The starting state required depends on whether the macro uses paint-bucket fills (indicated on the SETUP screen):

- **Brush-only macros** (no "Uses paint bucket" line): the canvas can have prior drawing on it. The macro will overdraw cleanly because every cell it intends to paint gets explicitly stamped. The cursor still needs to start at canvas center and the palette on pure black.
- **Macros that use paint bucket**: the canvas must be blank before the print starts, and the cursor must be at canvas center. Any prior drawing on the canvas would cause an error in the print. Use `convert.py --no-bucket` if you want to avoid this limitation.

### Runtime expectations

A 256×256 image typically takes 10–90 minutes depending on color count, geometry, and whether the generator schedules a paint-bucket pass. Pixel-art sprites with large solid regions can come in under 10 minutes; dense photographs with many colors can exceed 90 minutes. Runtime is printed by `convert.py` after generation and displayed on the SETUP screen before you commit to running.

### Switch 1 vs Switch 2

The printer works on both. Switch 2 polls its controllers at ~250 Hz wired; Switch 1 specs 125 Hz nominal but measures closer to ~75 Hz effective. Due to that or some other undiscovered reason, Switch 2 is less susceptible to a missed input distorting the end of long prints.

On Switch 1, prints up to ~65 minutes have run reliably. Prints over ~70 minutes can produce sporadic late-stage errors. Typically a small number of scattered pixels in colors that are drawn near the end of the macro are stamped in slightly incorrect places. Rerunning might produce a correct print, as this issue comes down to precise timing.

The only way to meaningfully reduce the risk on Switch 1 is to make the print shorter: simplify the source image, lower the unique color count, or use `--quant rgb` to avoid OKLAB's tendency to introduce additional palette entries. Switch 2 has reproduced clean prints up to 108 minutes. (Haven't tried a longer one.)