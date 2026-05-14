"""HORIPAD S 8-byte HID report assembly.

The descriptor registration lives in boot.py; this module just builds + sends
reports over the already-enabled usb_hid.Device.
"""
import usb_hid

BTN = {
    "Y":       0x0001,
    "B":       0x0002,
    "A":       0x0004,
    "X":       0x0008,
    "L":       0x0010,
    "R":       0x0020,
    "ZL":      0x0040,
    "ZR":      0x0080,
    "MINUS":   0x0100,
    "PLUS":    0x0200,
    "LSTICK":  0x0400,
    "RSTICK":  0x0800,
    "HOME":    0x1000,
    "CAPTURE": 0x2000,
}

HAT = {
    "DPAD_UP":         0,
    "DPAD_UP_RIGHT":   1,
    "DPAD_RIGHT":      2,
    "DPAD_DOWN_RIGHT": 3,
    "DPAD_DOWN":       4,
    "DPAD_DOWN_LEFT":  5,
    "DPAD_LEFT":       6,
    "DPAD_UP_LEFT":    7,
}
HAT_NEUTRAL = 8

STICK_DIR = {
    "LSTICK_UP":    (0, -1),
    "LSTICK_DOWN":  (0,  1),
    "LSTICK_LEFT":  (-1, 0),
    "LSTICK_RIGHT": ( 1, 0),
}


def stick_bytes(direction, magnitude):
    dx, dy = STICK_DIR[direction]
    magnitude = max(0.0, min(1.0, magnitude))
    lx = 0x80 + int(round(dx * magnitude * 0x7F))
    ly = 0x80 + int(round(dy * magnitude * 0x7F))
    return max(0, min(0xFF, lx)), max(0, min(0xFF, ly))


def report(buttons=0, hat=HAT_NEUTRAL, lx=0x80, ly=0x80):
    return bytes((
        buttons & 0xFF,
        (buttons >> 8) & 0xFF,
        hat,
        lx, ly, 0x80, 0x80,
        0x00,
    ))


NEUTRAL = report()


class HoriPad:
    def __init__(self):
        # usb_hid.devices is populated by boot.py's usb_hid.enable() call.
        # Only one HID device registered, so it's always index 0.
        self._dev = usb_hid.devices[0]

    def send(self, data):
        self._dev.send_report(data)

    def send_state(self, buttons=0, hat=HAT_NEUTRAL, lx=0x80, ly=0x80):
        self._dev.send_report(report(buttons, hat, lx, ly))

    def neutral(self):
        self._dev.send_report(NEUTRAL)
