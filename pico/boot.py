"""boot.py — runs before code.py on every CircuitPython boot.

Replaces CircuitPython's default keyboard-HID with a custom HORIPAD-S gamepad
descriptor, overrides the USB identity to match HORI's real VID/PID, and
disables USB mass storage so code.py can write to flash at runtime. Requires
CircuitPython 8.2+ for supervisor.set_usb_identification.
"""
import microcontroller
import storage
import supervisor
import usb_cdc
import usb_hid

import config


# UI writes 0xA5 to nvm[0] and calls microcontroller.reset() to request the
# "USB drive on next boot" equivalent of holding BTN_Y. We consume (clear)
# the flag here so the boot AFTER that returns to normal HID-only mode.
NVM_USB_DRIVE_FLAG = 0xA5
_nvm_request = False
try:
    if microcontroller.nvm[0] == NVM_USB_DRIVE_FLAG:
        _nvm_request = True
        microcontroller.nvm[0] = 0x00
except Exception as _e:
    print("boot: nvm read failed:", _e)

# HORIPAD S HID report descriptor — 86 bytes. Do not edit without re-testing:
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

# Switch won't recognize us as a HORIPAD S unless VID/PID match. Without this
# override, CircuitPython advertises its own VID and the Switch ignores us.
supervisor.set_usb_identification(
    manufacturer=config.USB_MANUFACTURER,
    product=config.USB_PRODUCT,
    vid=config.USB_VID,
    pid=config.USB_PID,
)

# MSC escape hatch: hold BTN_Y while the board is powered on to leave USB
# mass storage ENABLED for this boot. That makes the CIRCUITPY drive appear
# on the host so you can drag-drop firmware updates without needing safe mode
# or the UF2 bootloader. Release the button and reboot to return to normal
# mode (HTTP-only uploads, runtime filesystem writes re-enabled).
import time
import board
import digitalio

_override = False
try:
    _probe = digitalio.DigitalInOut(getattr(board, config.BTN_Y))
    _probe.direction = digitalio.Direction.INPUT
    _probe.pull = digitalio.Pull.UP
    time.sleep(0.02)  # settle the pull-up
    _override = not _probe.value  # active-low
    _probe.deinit()
except Exception as _e:
    print("boot: BTN_Y probe failed:", _e)

if _override or _nvm_request:
    reason = "BTN_Y held" if _override else "nvm flag set"
    print("boot:", reason, "- USB drive ENABLED")
else:
    # Normal mode: disable MSC so code.py can write to the filesystem at
    # runtime. Also hides CIRCUITPY from the Switch so it only sees the HID.
    storage.disable_usb_drive()

# Keep CDC console enabled — useful for debugging via serial over USB to a
# laptop. Switch ignores the CDC interface, so it doesn't affect HID enumeration.
usb_cdc.enable(console=True, data=False)
