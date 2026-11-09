"""Per-shutter BLE reception statistics for the diagnostic sensors.

For every device the tracker records:
- RSSI samples of DIRECT status frames (the ESP32 heard the shutter itself),
  per ESP32 node;
- the time of the last direct frame and of the last mesh-relayed frame.

Samples are averaged PER ESP32 over each PUBLISH_INTERVAL (at most one value
per minute per shutter, so the recorder is not flooded). The "Segnale BLE"
sensor shows the average of the ESP32 that hears the shutter best; its
attributes list the average of every ESP32 (v0.3.2: before, samples of
different ESP32s were mixed in one average); "Ricezione" tells whether the shutter is heard
directly ("diretta"), directly but weakly ("debole", average below
-90 dBm: frames often lost) or only through the mesh ("solo_mesh"). Shutters transmit only when they move,
so values update when the shutters are used.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

PUBLISH_INTERVAL = 60.0
RECEPTION_DIRECT = "diretta"
RECEPTION_WEAK = "debole"
RECEPTION_MESH = "solo_mesh"
RECEPTION_OPTIONS = [RECEPTION_DIRECT, RECEPTION_WEAK, RECEPTION_MESH]
WEAK_RSSI_DBM = -90  # direct, but average below this -> "debole"
DIRECT_MEMORY_S = 7 * 24 * 3600  # a direct sighting counts as "diretta" for a week


_UNNAMED = "?"


@dataclass
class _DeviceSignal:
    pending: dict[str, list[int]] = field(default_factory=dict)
    value: int | None = None
    value_ts: float = 0.0
    per_node: dict[str, int] = field(default_factory=dict)  # published averages
    best_node: str | None = None
    last_direct: float | None = None
    last_relay: float | None = None
    relay_via: str | None = None


class SignalTracker:
    """Collects BLE reception data; publishing is driven by flush()."""

    def __init__(self) -> None:
        self._dev: dict[str, _DeviceSignal] = {}

    def _get(self, uuid: str) -> _DeviceSignal:
        return self._dev.setdefault(uuid, _DeviceSignal())

    def note_direct(self, uuid: str, node: str | None, rssi: int, now: float | None = None) -> bool:
        """Record a direct frame. Returns True if a new value was published."""
        now = time.time() if now is None else now
        d = self._get(uuid)
        d.last_direct = now
        d.pending.setdefault(node or _UNNAMED, []).append(int(rssi))
        if d.value is None or now - d.value_ts >= PUBLISH_INTERVAL:
            return self._publish(d, now)
        return False

    def note_relay(self, uuid: str, repeater: str | None, now: float | None = None) -> bool:
        """Record a mesh-relayed frame. Returns True if the reception state changed."""
        now = time.time() if now is None else now
        d = self._get(uuid)
        before = self.reception(uuid, now)
        d.last_relay = now
        d.relay_via = repeater
        return self.reception(uuid, now) != before

    def flush(self, now: float | None = None) -> bool:
        """Publish pending samples older than PUBLISH_INTERVAL. True if anything changed."""
        now = time.time() if now is None else now
        changed = False
        for d in self._dev.values():
            if d.pending and now - d.value_ts >= PUBLISH_INTERVAL:
                changed |= self._publish(d, now)
        return changed

    @staticmethod
    def _publish(d: _DeviceSignal, now: float) -> bool:
        if not d.pending:
            return False
        for node, samples in d.pending.items():
            if samples:
                d.per_node[node] = round(sum(samples) / len(samples))
        d.pending.clear()
        named = {n: v for n, v in d.per_node.items() if n != _UNNAMED} or d.per_node
        best = max(named.items(), key=lambda kv: kv[1])
        d.best_node = None if best[0] == _UNNAMED else best[0]
        d.value = best[1]
        d.value_ts = now
        return True

    # ---- read side ---------------------------------------------------------

    def rssi(self, uuid: str) -> int | None:
        d = self._dev.get(uuid)
        return d.value if d else None

    def reception(self, uuid: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        d = self._dev.get(uuid)
        if d is None:
            return None
        if d.last_direct is not None and now - d.last_direct < DIRECT_MEMORY_S:
            if d.value is not None and d.value < WEAK_RSSI_DBM:
                return RECEPTION_WEAK
            return RECEPTION_DIRECT
        if d.last_relay is not None:
            return RECEPTION_MESH
        return None

    def attributes(self, uuid: str) -> dict:
        d = self._dev.get(uuid)
        if d is None:
            return {}
        fmt = lambda ts: None if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))  # noqa: E731
        return {
            "esp32_migliore": d.best_node,
            "rssi_per_esp32": {n: v for n, v in d.per_node.items() if n != _UNNAMED},
            "ultima_ricezione_diretta": fmt(d.last_direct),
            "ultima_ricezione_via_mesh": fmt(d.last_relay),
            "ripetitore_mesh": d.relay_via,
        }
