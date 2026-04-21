"""Hardware + network config for the portable Tomodachi printer on Pico 2 WH.

Pinout targets the Waveshare Pico-LCD-1.3 (240x240 ST7789 + joystick + 4 buttons).
Cross-check against the module silkscreen if pins don't match your board.
"""

# --- Waveshare Pico-LCD-1.3 pinout ---
LCD_DC   = "GP8"
LCD_CS   = "GP9"
LCD_SCK  = "GP10"
LCD_MOSI = "GP11"
LCD_RST  = "GP12"
LCD_BL   = "GP13"

BTN_A = "GP15"
BTN_B = "GP17"
BTN_X = "GP19"
BTN_Y = "GP21"

JOY_UP    = "GP2"
JOY_CTRL  = "GP3"
JOY_LEFT  = "GP16"
JOY_DOWN  = "GP18"
JOY_RIGHT = "GP20"

LCD_WIDTH  = 240
LCD_HEIGHT = 240
# Physical orientation: joystick left / buttons right when USB faces up.
# Adjust in 90-deg steps if "down" on screen doesn't match "down" on the module.
LCD_ROTATION = 270

# --- Storage ---
MACRO_DIR = "/macros"

# --- USB HID identity (HORIPAD S) ---
USB_VID = 0x0F0D
USB_PID = 0x0092
USB_MANUFACTURER = "HORI CO.,LTD."
USB_PRODUCT      = "HORIPAD S"
HID_REPORT_LENGTH = 8
