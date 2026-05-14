"""End-of-print push notification via ntfy.sh.

Joins home WiFi (station mode), POSTs a short message, returns. Called
from macro_runner.run_macro on successful completion. Missing config,
missing adafruit_requests, absent creds, or a network hiccup all degrade
silently — the notification is best-effort and never disturbs the print
pipeline.

Config lives in /secrets.txt as plain `KEY=VALUE` lines. See
secrets_example.txt for the expected keys.
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


def _load_secrets(path="/secrets.txt"):
    cfg = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


_secrets = _load_secrets()


def is_configured():
    """True iff deps loaded and both WIFI_SSID + NTFY_TOPIC are set in
    /secrets.txt. Used by the UI to decide whether to show a 'sending
    notification' status."""
    if not _DEPS_OK:
        return False
    return bool(_secrets.get("WIFI_SSID")) and bool(_secrets.get("NTFY_TOPIC"))


def send(message, title=None):
    if not _DEPS_OK:
        return False
    ssid = _secrets.get("WIFI_SSID")
    password = _secrets.get("WIFI_PASSWORD")
    topic = _secrets.get("NTFY_TOPIC")
    if not ssid or not topic:
        return False
    try:
        # Rebuild the association from scratch each call. After an idle
        # period the radio can hold one of three states (None, 0.0.0.0,
        # or a stale IP) and each needs a different recovery path.
        # Toggling `enabled` forces a clean slate before connect().
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
