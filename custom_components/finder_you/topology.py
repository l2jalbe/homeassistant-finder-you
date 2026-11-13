"""Topology and dynamic routing for Finder YESLY BLE mesh nodes.

Tracks link quality (RSSI) of all Finder YESLY devices seen by ESPHome nodes
distributed throughout the home, and routes local commands through the best hop.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback

if TYPE_CHECKING:
    from .api import BleDirectDevice

_LOGGER = logging.getLogger(__name__)

# Byte 6 of a 13S2 status frame: motor state.
# Observed: 0x10 while closing, 0x01 while opening, 0x00 when stopped.
SHUTTER_MOTION_FLAGS: dict[int, str | None] = {0x00: None, 0x01: "opening", 0x10: "closing"}

# RSSI considered strong enough for direct transmission
EXCELLENT_RSSI_THRESHOLD = -75
# Maximum age in seconds before an RSSI observation is considered stale
TOPOLOGY_STALE_TIMEOUT = 300.0
# Guard duration in seconds to ignore delayed or stale mesh telemetry after a local transmission
BLE_TX_GUARD_DURATION = 6.0

# Node selection is purely signal-based (see ROUTING.md), optionally
# overridden per shutter by the user ("ESP32 preferito" select, module
# preferred_nodes.py; up to v0.3.1 a YAML file in /config).
# The original author-specific maps (room adjacency, proxy MACs, room
# keywords) were removed in v0.1.4; they are in git history at bb921a6.


class FinderTopologyRouter:
    """Tracks ESPHome node visibility and selects the best transmitter for each device."""

    def __init__(self, hass: HomeAssistant, coordinator: Any = None) -> None:
        self.hass = hass
        self._coordinator = coordinator
        # device_mac -> {node_name: {"rssi": int, "last_seen": float, "mesh_hop": bool}}
        self._topology: dict[str, dict[str, dict[str, Any]]] = {}
        # Set of active ESPHome node names discovered on the network
        self._esphome_nodes: set[str] = set()
        # device_uuid -> timestamp until which conflicting telemetry updates are ignored
        self._tx_guard_until: dict[str, float] = {}
        # Deduplication cache for identical raw BLE payloads across dual listeners: mac -> (payload_tuple, ts)
        self._last_decoded_adv: dict[str, tuple[tuple[int, ...], float]] = {}
        # device_uuid -> how its last status frame arrived ("direct via n1",
        # "mesh relay <mac> via n1"); used for ack latency logging
        self.last_frame_source: dict[str, str] = {}
        self._unsub_listener = None
        self._unsub_bt_callback = None

    def start(self) -> None:
        """Start listening for BLE advertisement events and register known nodes."""
        self._install_bleak_finder_guard()
        self._unsub_listener = self.hass.bus.async_listen(
            "esphome.finder_ble_adv", self._handle_ble_adv_event
        )
        self._register_ha_bluetooth_listener()
        self._discover_registered_nodes()
        _LOGGER.info("Finder BLE Topology Router started")

    def stop(self) -> None:
        """Stop listening to events."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None
        if self._unsub_bt_callback:
            try:
                self._unsub_bt_callback()
            except Exception:
                pass
            self._unsub_bt_callback = None

    def _install_bleak_finder_guard(self) -> None:
        """Prevent HA Bluetooth from opening active GATT connections to connectionless Finder YESLY (05:61:*) devices.

        Finder YESLY actuators (13.S2, 13.72, 15.71) advertise as connectable peripherals without a GAP name,
        which causes habluetooth / active proxies to invoke BleakClient.connect() right after a shutter starts
        moving. While a 13.S2 actuator is held in an active GATT session, it stops broadcasting state packets
        and ignores connectionless BLE mesh commands.
        """
        try:
            import traceback
            from bleak.exc import BleakError
            from habluetooth.wrappers import HaBleakClientWrapper

            orig_connect = getattr(HaBleakClientWrapper, "connect", None)
            if orig_connect is None:
                return
            if getattr(orig_connect, "_finder_yesly_guarded", False):
                orig_connect = getattr(orig_connect, "_finder_yesly_orig", orig_connect)

            logged_caller = False

            async def _guarded_connect(client_self: Any, *args: Any, **kwargs: Any) -> bool:
                nonlocal logged_caller
                raw_addr = (
                    getattr(client_self, "_HaBleakClientWrapper__address", None)
                    or getattr(client_self, "_address", None)
                    or getattr(client_self, "address", None)
                    or ""
                )
                addr = str(raw_addr).upper().replace("-", ":")
                if addr.startswith("05:61:"):
                    if not logged_caller:
                        logged_caller = True
                        stack_str = "".join(traceback.format_stack(limit=10))
                        _LOGGER.info(
                            "Blocked active GATT connection attempt to Finder YESLY device %s. Caller stack:\n%s",
                            addr,
                            stack_str,
                        )
                    else:
                        _LOGGER.debug(
                            "Blocked active GATT connection attempt to connectionless Finder YESLY device %s",
                            addr,
                        )
                    raise BleakError(
                        f"Active GATT connections are disabled for connectionless Finder YESLY device {addr}"
                    )
                return await orig_connect(client_self, *args, **kwargs)

            setattr(_guarded_connect, "_finder_yesly_guarded", True)
            setattr(_guarded_connect, "_finder_yesly_orig", orig_connect)
            HaBleakClientWrapper.connect = _guarded_connect  # type: ignore[method-assign]
            _LOGGER.info("Installed BLE GATT connection guard for Finder YESLY (05:61:*) devices")
        except Exception as err:
            _LOGGER.debug("Could not install BLE GATT guard: %s", err)

    def _register_ha_bluetooth_listener(self) -> None:
        """Register passive listener on HA's native bluetooth integration to capture 05:61:* advertisements from all proxies (including rc-switch)."""
        try:
            from homeassistant.components import bluetooth
            from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher

            self._unsub_bt_callback = bluetooth.async_register_callback(
                self.hass,
                self._handle_ha_bluetooth_adv,
                BluetoothCallbackMatcher(connectable=False),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
            _LOGGER.info("Registered passive HA Bluetooth callback for Finder YESLY telemetry")
        except Exception as err:
            _LOGGER.debug("Could not register passive HA Bluetooth callback: %s", err)

    def _resolve_source_mac_to_node(self, source: str | None) -> str | None:
        """Map an HA Bluetooth scanner source MAC to a registered finder_mesh node.

        No static map: HA Bluetooth sightings are used for telemetry only
        (node=None); per-node RSSI comes from the ``esphome.finder_ble_adv``
        events, which carry the ESPHome device_id.
        """
        return None

    @callback
    def _handle_ha_bluetooth_adv(self, service_info: Any, change: Any) -> None:
        """Handle BLE advertisements captured by any HA Bluetooth proxy (including standard proxies like rc-switch)."""
        mac = getattr(service_info, "address", None)
        if not mac:
            return
        mac_clean = mac.upper().replace("-", ":")
        if not mac_clean.startswith("05:61:"):
            return

        mf_data = getattr(service_info, "manufacturer_data", None) or {}
        if not mf_data:
            return

        raw_bytes = mf_data.get(0x01D9) or mf_data.get(0x08F2)
        if raw_bytes is None:
            raw_bytes = next(iter(mf_data.values()), None)
        if not raw_bytes:
            return

        rssi = getattr(service_info, "rssi", None)
        rssi_int = int(rssi) if isinstance(rssi, (int, float)) else -95
        source = getattr(service_info, "source", None)
        node = self._resolve_source_mac_to_node(source)

        self._process_finder_adv_bytes(
            mac_clean=mac_clean,
            rssi=rssi_int,
            byte_list=list(raw_bytes),
            node=node,
            source_label=node or source or "ha_bt",
        )

    def _discover_registered_nodes(self) -> None:
        """Scan HA services to find ESPHome nodes that expose finder_mesh services."""
        services = self.hass.services.async_services()
        esphome_services = services.get("esphome", {})
        for s_name in esphome_services:
            if "finder_mesh" in s_name:
                parts = s_name.split("_finder_mesh_")
                if len(parts) == 2:
                    node_name = parts[0]
                    if node_name not in self._esphome_nodes:
                        self._esphome_nodes.add(node_name)
                        _LOGGER.info("Discovered compatible ESPHome mesh node: %s", node_name)

    @staticmethod
    def _norm(value: str | None) -> str:
        return (value or "").strip().lower().replace("-", "_").replace(" ", "_")

    def _esphome_device_node(self, dev_entry) -> str | None:
        """Exact node id of an ESPHome device registry entry, if registered.

        1. ``device_name`` stored by the ESPHome config entry (the YAML
           ``name:``, e.g. "finder-ble-2" -> "finder_ble_2");
        2. the device name / user name, normalised, equal to a node id.
        Never a partial or single-word match ("finder" would match every
        node: fixed in v0.3.2).
        """
        # A device belongs to a single config entry; ``config_entries`` is
        # deprecated (removed in HA 2027.10). Older cores only have the set.
        if hasattr(dev_entry, "config_entry_id"):
            entry_ids = (dev_entry.config_entry_id,)
        else:
            entry_ids = tuple(dev_entry.config_entries)
        for entry_id in entry_ids:
            if entry_id is None:
                continue
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == "esphome":
                node = self._norm(entry.data.get("device_name"))
                if node in self._esphome_nodes:
                    return node
        for name in (dev_entry.name, dev_entry.name_by_user):
            node = self._norm(name)
            if node in self._esphome_nodes:
                return node
        return None

    def _resolve_esphome_node(self, node: str | None, device_id: str | None) -> str | None:
        """Resolve the ESPHome node name from an explicit node name or device_id."""
        if node and node in self._esphome_nodes:
            return node
        if not device_id:
            return None
        if not hasattr(self, "_dev_id_to_node"):
            self._dev_id_to_node: dict[str, str] = {}
        cached = self._dev_id_to_node.get(device_id)
        if cached and cached in self._esphome_nodes:
            return cached
        try:
            from homeassistant.helpers import device_registry as dr

            dev_entry = dr.async_get(self.hass).async_get(device_id)
            if dev_entry is None:
                return None
            matched = self._esphome_device_node(dev_entry)
            if matched:
                self._dev_id_to_node[device_id] = matched  # failures are not cached
                _LOGGER.info(
                    "Resolved ESPHome device '%s' (device_id %s) -> node '%s'",
                    dev_entry.name_by_user or dev_entry.name,
                    device_id,
                    matched,
                )
            else:
                if not hasattr(self, "_unresolved_logged"):
                    self._unresolved_logged: set[str] = set()
                if device_id not in self._unresolved_logged:
                    self._unresolved_logged.add(device_id)
                    _LOGGER.warning(
                        "ESPHome device '%s' sends Finder frames but matches no finder_mesh node %s",
                        dev_entry.name_by_user or dev_entry.name,
                        sorted(self._esphome_nodes),
                    )
            return matched
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed resolving device_id '%s': %s", device_id, err)
        return None

    def node_online(self, node: str) -> bool:
        """True while the node's finder_mesh service is registered (device connected)."""
        return self.hass.services.has_service("esphome", f"{node}_finder_mesh_set_percent")

    def online_nodes(self) -> set[str]:
        """Detected nodes whose finder_mesh services are currently registered."""
        self._discover_registered_nodes()
        return {n for n in self._esphome_nodes if self.node_online(n)}

    def node_labels(self) -> dict[str, str]:
        """node id -> name of the ESPHome device as shown in HA (user rename included)."""
        from homeassistant.helpers import device_registry as dr

        self._discover_registered_nodes()
        labels = {n: n for n in self._esphome_nodes}
        registry = dr.async_get(self.hass)
        for entry in self.hass.config_entries.async_entries("esphome"):
            for dev in dr.async_entries_for_config_entry(registry, entry.entry_id):
                node = self._esphome_device_node(dev)
                if node:
                    labels[node] = dev.name_by_user or dev.name or node
        return labels

    @callback
    def _resolve_relay_origin(self, byte_list: list[int]) -> str | None:
        """Return the MAC of the device a mesh-relayed status frame is about.

        Matches bytes 0-3 (bit 0x40 of byte 0 cleared) against the last four
        MAC bytes of the plant inventory; returns None when nothing matches,
        so unknown/encrypted relay frames keep being ignored.
        """
        if not self._coordinator or not hasattr(self._coordinator, "ble_inventory"):
            return None
        suffix = [byte_list[0] & ~0x40 & 0xFF, byte_list[1], byte_list[2], byte_list[3]]
        for dev in self._coordinator.ble_inventory.values():
            parts = dev.mac.upper().replace("-", ":").split(":")
            if len(parts) != 6:
                continue
            try:
                tail = [int(p, 16) for p in parts[2:]]
            except ValueError:
                continue
            if tail == suffix:
                return ":".join(parts)
        return None

    @callback
    def _handle_ble_adv_event(self, event: Event) -> None:
        """Process incoming BLE advertisement observation from an ESPHome node."""
        data = event.data
        raw_node = data.get("node")
        device_id = data.get("device_id")
        node = self._resolve_esphome_node(raw_node, device_id)
        mac = data.get("mac")
        rssi_str = data.get("rssi")
        mf_str = data.get("mf", "")

        if not mac or not rssi_str:
            return

        try:
            rssi = int(rssi_str)
        except (ValueError, TypeError):
            return

        if node:
            self._esphome_nodes.add(node)
        mac_clean = mac.upper().replace("-", ":")

        # Parse manufacturer hex bytes
        byte_list: list[int] = []
        parts = mf_str.strip().split()
        if parts:
            for p in parts:
                try:
                    byte_list.append(int(p, 16))
                except ValueError:
                    pass

        self._process_finder_adv_bytes(
            mac_clean=mac_clean,
            rssi=rssi,
            byte_list=byte_list,
            node=node,
            source_label=node or "unknown",
        )

    def _process_finder_adv_bytes(
        self,
        mac_clean: str,
        rssi: int,
        byte_list: list[int],
        node: str | None,
        source_label: str = "unknown",
    ) -> None:
        """Common topology and state telemetry processor for Finder YESLY BLE advertisements."""
        # 15-byte frames are button/scenario group commands (encrypted):
        # hand them to the group supervisor (learning + recovery).
        if len(byte_list) == 15 and self._coordinator is not None:
            supervisor = getattr(self._coordinator, "group_supervisor", None)
            if supervisor is not None:
                supervisor.handle_command_frame(byte_list)
        is_mesh_relay = False
        if byte_list and (byte_list[0] & 0x40) != 0:
            is_mesh_relay = True

        # Mesh-relayed STATUS frames carry the originating device in their
        # first 4 bytes (= last 4 bytes of its MAC, with bit 0x40 set on the
        # first one) and the same plaintext payload as the direct frame, e.g.
        #   direct  05:61:06:00:F3:EB  06 00 F3 EB B1 3D 00 00 42 01 13
        #   relay   05:61:03:20:F2:3E  46 00 F3 EB B1 3D 00 00 42 01 13
        # Resolve the origin so a single ESP32 node also receives the state
        # of shutters it cannot hear directly.
        relay_origin = None
        if is_mesh_relay and len(byte_list) >= 11:
            relay_origin = self._resolve_relay_origin(byte_list)

        now = time.time()
        if node:
            node_entry = self._topology.setdefault(mac_clean, {}).setdefault(node, {})
            node_entry["rssi"] = rssi
            node_entry["last_seen"] = now
            node_entry["is_mesh_relay"] = is_mesh_relay

        if is_mesh_relay and relay_origin is None:
            _LOGGER.debug(
                "Ignoring mesh relay frame (0x4x, %d bytes) from repeater %s via '%s'",
                len(byte_list),
                mac_clean,
                source_label,
            )
            return

        repeater_mac = None
        if relay_origin is not None:
            repeater_mac = mac_clean
            mac_clean = relay_origin
            if node:
                self._topology.setdefault(mac_clean, {}).setdefault(node, {})[
                    "last_relay_seen"
                ] = now
            _LOGGER.debug(
                "Mesh relay status frame for %s via repeater %s seen by '%s' (RSSI %d)",
                mac_clean,
                repeater_mac,
                source_label,
                rssi,
            )

        _LOGGER.debug(
            "BLE Topology updated: MAC %s seen by '%s' (RSSI: %d dBm)",
            mac_clean,
            source_label,
            rssi,
        )

        # Telemetry decoding: match direct frame by exact MAC with device inventory in coordinator
        if self._coordinator and hasattr(self._coordinator, "ble_inventory"):
            inv = self._coordinator.ble_inventory
            matched_dev = None

            for dev in inv.values():
                if dev.mac.upper().replace("-", ":") == mac_clean:
                    matched_dev = dev
                    break

            if matched_dev:
                # Do not interpret command broadcast frames as state telemetry!
                # ESPHome / Gateway command packets are 9 bytes: [4B Counter] [1B Cmd] [4B AES-CCM MIC].
                # Decompiled BleDeviceSupport.GetDeviceChannelStatus requires data.Length >= 11 for telemetry!
                if len(byte_list) < 11:
                    _LOGGER.debug(
                        "Ignoring short/command BLE packet (%d bytes) for %s from '%s'",
                        len(byte_list),
                        matched_dev.mac,
                        source_label,
                    )
                    return

                if node and repeater_mac is None:
                    self._topology.setdefault(mac_clean, {}).setdefault(node, {})["last_telemetry"] = now

                # Signal statistics BEFORE de-duplication: when two ESP32s hear
                # the same frame, both RSSI values count (v0.3.2).
                tracker = getattr(self._coordinator, "signal_tracker", None)
                if tracker is not None:
                    if repeater_mac:
                        changed = tracker.note_relay(matched_dev.uuid, repeater_mac, now)
                    else:
                        changed = tracker.note_direct(matched_dev.uuid, node or source_label, rssi, now)
                    if changed:
                        self._coordinator.async_update_listeners()

                # Deduplicate identical frames within 200ms across dual listeners
                # and across direct/relayed copies (byte 0 differs by the 0x40
                # relay flag, so it is excluded from the key).
                payload_key = tuple(byte_list[1:10])
                prev_dec = self._last_decoded_adv.get(mac_clean)
                if prev_dec is not None and prev_dec[0] == payload_key and (now - prev_dec[1]) < 0.2:
                    return
                self._last_decoded_adv[mac_clean] = (payload_key, now)

                channel_states: dict[int, bool] = {}
                percent_val: int | None = None
                motion_val: str | None = None
                self.last_frame_source[matched_dev.uuid] = (
                    f"mesh relay {repeater_mac} via '{source_label}'"
                    if repeater_mac
                    else f"direct via '{source_label}'"
                )
                fam = (matched_dev.family or "").upper()
                is_dimmer = any(k in fam for k in ["1521", "1571"])
                is_shutter = any(k in fam for k in ["13S2", "SHUTTER"])

                if is_dimmer:
                    if len(byte_list) > 4:
                        raw_pos = byte_list[4]
                        if 0 <= raw_pos <= 100:
                            percent_val = raw_pos
                elif is_shutter:
                    if len(byte_list) > 5:
                        raw_pos = byte_list[5]
                        if 0 <= raw_pos <= 100:
                            percent_val = raw_pos
                    if len(byte_list) > 6:
                        motion_val = SHUTTER_MOTION_FLAGS.get(byte_list[6])
                else:
                    status_byte: int | None = byte_list[6] if len(byte_list) >= 7 else None
                    if status_byte is not None:
                        channel_states[1] = bool(status_byte & 0x01)
                        channel_states[2] = bool(status_byte & 0x10)

                if is_shutter and len(byte_list) > 6:
                    self._coordinator.handle_ble_shutter_motion(matched_dev.uuid, motion_val)

                if channel_states or percent_val is not None:
                    self._coordinator.handle_ble_device_state_update(
                        matched_dev.uuid,
                        channel_states=channel_states if channel_states else None,
                        percent=percent_val,
                    )

    def has_direct_telemetry(self, device: BleDirectDevice, max_age: float = 1800.0) -> bool:
        """Return True if any active ESPHome node has received direct BLE telemetry from this device."""
        mac_upper = device.mac.upper().replace("-", ":")
        now = time.time()
        candidates = self._topology.get(mac_upper, {})
        for node, info in candidates.items():
            if node in self._esphome_nodes and (now - info.get("last_telemetry", 0)) < max_age:
                return True
        if mac_upper in self._last_decoded_adv and (now - self._last_decoded_adv[mac_clean if (mac_clean := mac_upper) else mac_upper][1]) < max_age:
            return True
        return False

    def get_node_rssi(self, device: BleDirectDevice, node: str | None) -> int | None:
        """Return the last recorded RSSI (in dBm) of `device` on `node`, or None if unknown."""
        if not node:
            return None
        mac_upper = device.mac.upper().replace("-", ":")
        info = self._topology.get(mac_upper, {}).get(node)
        if info and "rssi" in info:
            return int(info["rssi"])
        return None

    @property
    def esphome_nodes(self) -> set[str]:
        return set(self._esphome_nodes)

    def _user_preferred_node(self, device: BleDirectDevice) -> str | None:
        """Node chosen by the user in the "ESP32 preferito" select, if online."""
        prefs = getattr(self._coordinator, "preferred_nodes", None) if self._coordinator else None
        if prefs is None:
            return None
        return prefs.preferred_node(device.uuid, self.online_nodes())

    def select_best_node(self, device: BleDirectDevice, channel: int | None = None) -> str | None:
        """Select the ESPHome node to transmit to the target device.

        Order (see ROUTING.md):
        1. node chosen in the shutter's "ESP32 preferito" select (if online);
        2. best fresh direct RSSI (< TOPOLOGY_STALE_TIMEOUT);
        3. best historical direct RSSI (stationary wall devices);
        4. node with the best fresh RSSI towards ANY Finder device
           (the mesh will relay the command);
        5. first available node.
        """
        self._discover_registered_nodes()
        if not self._esphome_nodes:
            return None

        mac_upper = device.mac.upper().replace("-", ":")
        now = time.time()

        forced = self._user_preferred_node(device)
        if forced:
            _LOGGER.info("Selected node '%s' for MAC %s from user routing map", forced, mac_upper)
            return forced

        candidates = self._topology.get(mac_upper, {})
        fresh = sorted(
            (
                (node, info.get("rssi", -100))
                for node, info in candidates.items()
                if node in self._esphome_nodes
                and "rssi" in info
                and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        if fresh:
            best_node, best_rssi = fresh[0]
            _LOGGER.info(
                "Selected best node '%s' for MAC %s based on direct RSSI (%d dBm)",
                best_node,
                mac_upper,
                best_rssi,
            )
            return best_node

        hist = sorted(
            (
                (node, info.get("rssi", -100))
                for node, info in candidates.items()
                if node in self._esphome_nodes and "rssi" in info
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        if hist:
            return hist[0][0]

        # Not heard directly: pick the node with the best view of the mesh
        mesh_scores: dict[str, int] = {}
        for _dev_mac, nodes in self._topology.items():
            for node, info in nodes.items():
                if (
                    node in self._esphome_nodes
                    and "rssi" in info
                    and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT
                ):
                    mesh_scores[node] = max(mesh_scores.get(node, -100), info.get("rssi", -100))
        if mesh_scores:
            return max(mesh_scores.items(), key=lambda x: x[1])[0]

        return sorted(self._esphome_nodes)[0]

    def select_alternate_node(
        self,
        device: BleDirectDevice,
        exclude_node: str | None,
        channel: int | None = None,
    ) -> str | None:
        """Select the best node other than ``exclude_node`` (signal-based)."""
        self._discover_registered_nodes()
        available = [n for n in self._esphome_nodes if n != exclude_node]
        if not available:
            return None

        mac_upper = device.mac.upper().replace("-", ":")
        now = time.time()
        candidates = self._topology.get(mac_upper, {})

        # 1. Fresh direct RSSI
        fresh = [
            (node, info.get("rssi", -100))
            for node, info in candidates.items()
            if node in available and "rssi" in info and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT
        ]
        if fresh:
            return max(fresh, key=lambda x: x[1])[0]

        # 2. Historical direct RSSI
        hist = [(node, info.get("rssi", -100)) for node, info in candidates.items() if node in available and "rssi" in info]
        if hist:
            return max(hist, key=lambda x: x[1])[0]

        # 3. Best view of the mesh, then first available
        mesh_scores: dict[str, int] = {}
        for _dev_mac, nodes in self._topology.items():
            for node, info in nodes.items():
                if node in available and "rssi" in info and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT:
                    mesh_scores[node] = max(mesh_scores.get(node, -100), info.get("rssi", -100))
        if mesh_scores:
            return max(mesh_scores.items(), key=lambda x: x[1])[0]
        return sorted(available)[0]

    async def async_dispatch_click(
        self,
        device: BleDirectDevice,
        channel: int,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a relay / button click via the best ESPHome node or alternate node."""
        self._discover_registered_nodes()
        if not self._esphome_nodes:
            _LOGGER.warning("No ESPHome mesh node available to send click to %s", device.mac)
            return False

        payload = {
            "mac": device.mac,
            "key": device.key,
            "channel": channel,
        }

        target_node = preferred_node or self.select_best_node(device, channel=channel)

        if target_node:
            service_name = f"{target_node}_finder_mesh_click"
            try:
                _LOGGER.info(
                    "Local BLE Dispatch: Calling esphome.%s for MAC %s Ch %d",
                    service_name,
                    device.mac,
                    channel,
                )
                await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
                self._tx_guard_until[device.uuid] = time.time() + BLE_TX_GUARD_DURATION
                return True
            except Exception as err:
                _LOGGER.warning(
                    "Failed to send local BLE click via node '%s': %s", target_node, err
                )

        alt_node = self.select_alternate_node(device, exclude_node=target_node, channel=channel)
        if alt_node:
            alt_service = f"{alt_node}_finder_mesh_click"
            try:
                _LOGGER.info(
                    "Retrying local BLE click via alternate node '%s'", alt_node
                )
                await self.hass.services.async_call("esphome", alt_service, payload, blocking=True)
                self._tx_guard_until[device.uuid] = time.time() + BLE_TX_GUARD_DURATION
                return True
            except Exception as alt_err:
                _LOGGER.warning("Alternate node '%s' also failed: %s", alt_node, alt_err)
        return False

    async def async_dispatch_set_percent(
        self,
        device: BleDirectDevice,
        percent: int,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a dimmer or shutter position via the best ESPHome node."""
        target_node = preferred_node or self.select_best_node(device)
        if not target_node:
            _LOGGER.warning("No ESPHome mesh node available to set percent on %s", device.mac)
            return False

        service_name = f"{target_node}_finder_mesh_set_percent"
        payload = {
            "mac": device.mac,
            "key": device.key,
            "percent": percent,
        }
        try:
            _LOGGER.info(
                "Local BLE Dispatch: Calling esphome.%s for MAC %s Percent %d%%",
                service_name,
                device.mac,
                percent,
            )
            await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
            self._tx_guard_until[device.uuid] = time.time() + BLE_TX_GUARD_DURATION
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to send local BLE percent via node '%s': %s", target_node, err
            )
            alt_node = self.select_alternate_node(device, exclude_node=target_node)
            if alt_node:
                alt_service = f"{alt_node}_finder_mesh_set_percent"
                try:
                    _LOGGER.info(
                        "Retrying local BLE percent via alternate node '%s'", alt_node
                    )
                    await self.hass.services.async_call("esphome", alt_service, payload, blocking=True)
                    self._tx_guard_until[device.uuid] = time.time() + BLE_TX_GUARD_DURATION
                    return True
                except Exception as alt_err:
                    _LOGGER.warning("Alternate node '%s' also failed: %s", alt_node, alt_err)
            return False

    async def async_dispatch_shutter_stop(
        self,
        device: BleDirectDevice,
        channel: int = 1,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a shutter stop command via the best ESPHome node."""
        target_node = preferred_node or self.select_best_node(device)
        if not target_node:
            return False

        service_name = f"{target_node}_finder_mesh_shutter_stop"
        payload = {
            "mac": device.mac,
            "key": device.key,
            "channel": channel,
        }
        try:
            _LOGGER.info(
                "Local BLE Dispatch: Calling esphome.%s for MAC %s Ch %d",
                service_name,
                device.mac,
                channel,
            )
            await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to send local BLE stop via node '%s': %s", target_node, err
            )
            alt_node = self.select_alternate_node(device, exclude_node=target_node)
            if alt_node:
                alt_service = f"{alt_node}_finder_mesh_shutter_stop"
                try:
                    _LOGGER.info(
                        "Retrying local BLE stop via alternate node '%s'", alt_node
                    )
                    await self.hass.services.async_call("esphome", alt_service, payload, blocking=True)
                    return True
                except Exception as alt_err:
                    _LOGGER.warning("Alternate node '%s' stop also failed: %s", alt_node, alt_err)
            return False
