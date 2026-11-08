"""Constants for the Finder YOU integration."""

from __future__ import annotations

DOMAIN = "finder_you"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_PLANT_ID = "plant_id"
CONF_ENABLE_LOCAL_BLE = "enable_local_ble"
CONF_STABLE_SESSION = "stable_cloud_session"

DEFAULT_SCAN_INTERVAL_SECONDS = 30
DEFAULT_ENABLE_LOCAL_BLE = True
# Stable cloud session: never tear down the cloud connection proactively.
# Needed on plants (e.g. gateway 1Y.GU.005) whose cloud never pushes
# shutter telemetry: the integration would otherwise rehandshake before
# every command, and the gateway only stays bound to the first session.
DEFAULT_STABLE_SESSION = False
STABLE_SCAN_INTERVAL_SECONDS = 120
# Consecutive poll timeouts tolerated in stable mode before the client
# is dropped and the connection rebuilt.
STABLE_MAX_POLL_FAILURES_BEFORE_DROP = 3

PLATFORMS: list[str] = ["cover", "sensor", "scene", "light", "switch", "button", "select"]
