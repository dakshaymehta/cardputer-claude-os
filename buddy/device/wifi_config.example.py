"""WiFi credentials for the launcher's boot auto-connect.

Copy to ``wifi_config.py`` (gitignored) and fill in. ``wifi_event.py``
imports it at boot; leave either value empty to skip the connect.
The Cardputer's ESP32-S3 only speaks 2.4 GHz — a 5 GHz-only SSID
will never associate.
"""

SSID = ""
PASSWORD = ""
