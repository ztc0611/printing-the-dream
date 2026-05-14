"""boot.py — runs before code.py on every CircuitPython boot.

Registers the HORIPAD-S HID identity, overrides VID/PID, decides whether
to expose the USB mass-storage drive, and enables the CDC console.
Requires CircuitPython 8.2+.
"""
import microcontroller
import storage
import supervisor
import usb_cdc
import usb_hid

import config


# UI sets nvm[0] to request MSC on the next boot (equivalent to holding Y).
NVM_USB_DRIVE_FLAG = 0xA5
_nvm_request = False
try:
    if microcontroller.nvm[0] == NVM_USB_DRIVE_FLAG:
        _nvm_request = True
        microcontroller.nvm[0] = 0x00
except Exception as _e:
    print("boot: nvm read failed:", _e)

# HORIPAD S HID report descriptor — 86 bytes. Don't edit without re-testing;
# the Switch validates this against its accepted-gamepad list.
HORIPAD_REPORT_DESCRIPTOR = bytes((
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Gamepad)
    0xA1, 0x01,        # Collection (Application)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x35, 0x00,        #   Physical Minimum (0)
    0x45, 0x01,        #   Physical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x10,        #   Report Count (16 buttons)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (1)
    0x29, 0x10,        #   Usage Maximum (16)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x25, 0x07,        #   Logical Maximum (7)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x65, 0x14,        #   Unit (Degrees, SI Rotation)
    0x09, 0x39,        #   Usage (Hat switch)
    0x81, 0x42,        #   Input (Data, Var, Abs, Null state)
    0x65, 0x00,        #   Unit (none)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x01,        #   Input (Const)          padding for 4-bit hat
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x46, 0xFF, 0x00,  #   Physical Maximum (255)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4 sticks)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x06, 0x00, 0xFF,  #   Usage Page (vendor-defined)
    0x09, 0x20,        #   Usage (0x20)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Var, Abs)  vendor byte
    0x0A, 0x21, 0x26,  #   Usage (vendor)
    0x95, 0x08,        #   Report Count (8)
    0x91, 0x02,        #   Output (Data, Var, Abs) rumble/LED, unused
    0xC0,              # End Collection
))

gamepad = usb_hid.Device(
    report_descriptor=HORIPAD_REPORT_DESCRIPTOR,
    usage_page=0x01,
    usage=0x05,
    report_ids=(0,),
    in_report_lengths=(config.HID_REPORT_LENGTH,),
    out_report_lengths=(8,),
)

usb_hid.enable((gamepad,))

# Override VID/PID so the Switch recognizes us as HORIPAD S.
supervisor.set_usb_identification(
    manufacturer=config.USB_MANUFACTURER,
    product=config.USB_PRODUCT,
    vid=config.USB_VID,
    pid=config.USB_PID,
)

# Ensure /macros exists so the host sees it as a folder when MSC mounts.
try:
    import os
    os.mkdir("/macros")
except OSError:
    pass

# MSC trigger: BTN_Y held, nvm flag set, or /macros is empty.
import time
import board
import digitalio

_override = False
try:
    _probe = digitalio.DigitalInOut(getattr(board, config.BTN_Y))
    _probe.direction = digitalio.Direction.INPUT
    _probe.pull = digitalio.Pull.UP
    time.sleep(0.02)
    _override = not _probe.value
    _probe.deinit()
except Exception as _e:
    print("boot: BTN_Y probe failed:", _e)

_macros_empty = True
try:
    import os
    _files = os.listdir("/macros")
    _macros_empty = not any(
        f.endswith(".mz") or f.endswith(".txt") for f in _files
    )
except OSError:
    _macros_empty = True

if _override or _nvm_request or _macros_empty:
    print("boot: USB drive ENABLED")
else:
    storage.disable_usb_drive()

usb_cdc.enable(console=True, data=False)
