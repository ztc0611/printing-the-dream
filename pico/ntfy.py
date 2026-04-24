"""End-of-print push notification via ntfy.sh.

Joins home WiFi (station mode), POSTs a short message, returns. Called
from macro_runner.run_macro on successful completion. Missing secrets.py,
missing adafruit_requests, absent creds, or a network hiccup all degrade
silently — the notification is best-effort and never disturbs the print
pipeline.
"""
try:
    import wifi
    import socketpool
    import ssl
    import adafruit_requests
    _DEPS_OK = True
except ImportError as e:
    print("ntfy: deps missing:", e)
    _DEPS_OK = False

try:
    import secrets as _secrets
except ImportError:
    _secrets = None


def is_configured():
    """True iff the deps loaded, secrets.py exists, and both WIFI_SSID and
    NTFY_TOPIC are actually set. Used by the UI to decide whether to bother
    showing a "sending notification" status."""
    if not _DEPS_OK or _secrets is None:
        return False
    ssid = getattr(_secrets, "WIFI_SSID", None)
    topic = getattr(_secrets, "NTFY_TOPIC", None)
    return bool(ssid) and bool(topic)


def send(message, title=None):
    if not _DEPS_OK or _secrets is None:
        return False
    ssid = getattr(_secrets, "WIFI_SSID", None)
    password = getattr(_secrets, "WIFI_PASSWORD", None)
    topic = getattr(_secrets, "NTFY_TOPIC", None)
    if not ssid or not topic:
        return False
    try:
        # Rebuild the association from scratch each call. After an idle
        # period the radio can hold one of three states (None, 0.0.0.0, or
        # a stale IP) and each needs a different recovery path. Toggling
        # `enabled` forces a clean slate before connect().
        wifi.radio.enabled = False
        wifi.radio.enabled = True
        wifi.radio.connect(ssid, password, timeout=8)
        pool = socketpool.SocketPool(wifi.radio)
        session = adafruit_requests.Session(pool, ssl.create_default_context())
        headers = {}
        if title:
            headers["Title"] = title
        resp = session.post(
            "https://ntfy.sh/" + topic,
            data=message,
            headers=headers,
            timeout=5,
        )
        resp.close()
        return True
    except Exception as e:
        print("ntfy send failed:", e)
        return False
