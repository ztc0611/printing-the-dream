"""Optional wifi + ntfy.sh push notification on print completion.

Off by default — the printer runs identically whether this file is present
or not. Leaving the values as None also disables ntfy.

To enable:
  1. Rename this file to secrets.py on your CIRCUITPY drive.
  2. Fill in your wifi creds and an ntfy topic.
  3. Drop adafruit_requests into CIRCUITPY/lib/.
"""
WIFI_SSID = None
WIFI_PASSWORD = None
# ntfy.sh topics are public by name — pick something unguessable.
NTFY_TOPIC = None
