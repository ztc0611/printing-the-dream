"""ST7789 init for the Waveshare Pico-LCD-1.3 (240x240, IPS, ST7789VW, SPI).

Returns the Display object. The UI owns all scene graph content — this
module is intentionally tiny. If the adafruit_st7789 library isn't in /lib,
init() returns None and the UI falls back to console prints.

Pinout sourced from the Waveshare wiki (MOSI=GP11, SCK=GP10, CS=GP9,
DC=GP8, RST=GP12, BL=GP13).
"""
import board
import busio
import displayio

import config

try:
    from adafruit_st7789 import ST7789
    # CP 9+ split FourWire out of displayio into its own module.
    from fourwire import FourWire
    _HAVE_LIB = True
except ImportError:
    _HAVE_LIB = False


def _pin(name):
    return getattr(board, name)


def init():
    if not _HAVE_LIB:
        print("adafruit_st7789 not installed — running headless")
        return None
    displayio.release_displays()
    spi = busio.SPI(clock=_pin(config.LCD_SCK), MOSI=_pin(config.LCD_MOSI))
    bus = FourWire(
        spi,
        command=_pin(config.LCD_DC),
        chip_select=_pin(config.LCD_CS),
        reset=_pin(config.LCD_RST),
        baudrate=62_500_000,
    )
    # rowstart=80 compensates for the ST7789V's 240x320 GRAM when we override
    # MADCTL via rotation. Waveshare's factory MADCTL maps 0..239 directly but
    # adafruit_st7789 replaces it, so we need the offset to land on the
    # visible 240 rows again.
    return ST7789(
        bus,
        width=config.LCD_WIDTH,
        height=config.LCD_HEIGHT,
        rowstart=80,
        colstart=0,
        rotation=config.LCD_ROTATION,
        backlight_pin=_pin(config.LCD_BL),
    )
