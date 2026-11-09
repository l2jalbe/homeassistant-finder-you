"""Per-shutter preferred ESP32 node, chosen from the "ESP32 preferito" select.

Replaces the YAML routing map (``/config/finder_you_mappa_personalizzata.yaml``,
v0.1.5 - v0.3.1). Choices are kept in HA storage
(``.storage/finder_you_preferred_nodes``), so they survive HACS updates and
restarts and need no file editing.

- The stored value is the ESPHome node id (``finder_ble_2``), never the
  display name: renaming the ESP32 device in HA does not break the choice.
- Every ESP32 ever seen is remembered with its display name, so a node that
  is offline is still listed (as "<name> (non disponibile)") and the choice
  is kept; while it is offline the shutter behaves as "auto".
- On first start the values of the old YAML file are imported once and the
  file is renamed ``<file>.importato`` (it is no longer created).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "finder_you_preferred_nodes"
STORAGE_VERSION = 1
SIGNAL_PREFERRED_UPDATED = "finder_you_preferred_nodes_updated"
LEGACY_FILE_NAME = "finder_you_mappa_personalizzata.yaml"
IMPORTED_SUFFIX = ".importato"
AUTO = "auto"


def normalize_node(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def read_legacy_file(path: str) -> dict[str, str]:
    """Blocking: shutter name -> node id from the old YAML file (auto skipped)."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    section = data.get("tapparelle") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    if isinstance(section, dict):
        for name, value in section.items():
            if name is None or value is None:
                continue
            node = normalize_node(str(value))
            if node and node != AUTO:
                out[str(name).strip()] = node
    return out


class PreferredNodes:
    """Storage of the user's preferred ESP32 per shutter."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._prefs: dict[str, str] = {}  # shutter uuid -> node id
        self._known: dict[str, str] = {}  # node id -> display name
        self._legacy_done = False
        self._warned: set[tuple[str, str]] = set()

    # ---- persistence -------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._prefs = dict(data.get("prefs", {}) or {})
            self._known = dict(data.get("known_nodes", {}) or {})
            self._legacy_done = bool(data.get("legacy_imported", False))

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "prefs": self._prefs,
                "known_nodes": self._known,
                "legacy_imported": self._legacy_done,
            },
            5,
        )

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_PREFERRED_UPDATED)

    async def async_import_legacy(self, shutters: list[tuple[str, str]]) -> None:
        """Import the old YAML routing map once, then rename it.

        ``shutters`` is a list of (name, uuid). Values already set from the
        select are never overwritten.
        """
        if self._legacy_done:
            return
        path = self.hass.config.path(LEGACY_FILE_NAME)
        if not await self.hass.async_add_executor_job(os.path.exists, path):
            self._legacy_done = True
            self._save()
            return
        try:
            by_name = await self.hass.async_add_executor_job(read_legacy_file, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cannot read old routing map %s (%s): not imported", path, err)
            by_name = {}
        imported = 0
        for name, uuid in shutters:
            for key in ((name or "").strip(), f"{(name or '').strip()} ({uuid[:8]})"):
                node = by_name.get(key)
                if node and uuid not in self._prefs:
                    self._prefs[uuid] = node
                    self._known.setdefault(node, node)
                    imported += 1
                    break
        try:
            await self.hass.async_add_executor_job(os.replace, path, path + IMPORTED_SUFFIX)
        except OSError as err:
            _LOGGER.warning("Cannot rename %s (%s)", path, err)
        self._legacy_done = True
        self._save()
        self._notify()
        _LOGGER.info(
            "Imported %d preferred ESP32 from %s (renamed to %s%s); "
            "use the 'ESP32 preferito' select of each shutter from now on",
            imported,
            LEGACY_FILE_NAME,
            LEGACY_FILE_NAME,
            IMPORTED_SUFFIX,
        )

    # ---- nodes -------------------------------------------------------------

    @callback
    def remember_nodes(self, labels: dict[str, str]) -> None:
        """Record detected nodes and their current display names."""
        changed = False
        for node, label in labels.items():
            if self._known.get(node) != label:
                self._known[node] = label
                changed = True
        if changed:
            self._save()
            self._notify()

    def known_nodes(self) -> dict[str, str]:
        return dict(self._known)

    # ---- preferences -------------------------------------------------------

    def get(self, uuid: str) -> str | None:
        return self._prefs.get(uuid)

    @callback
    def set(self, uuid: str, node: str | None) -> None:
        if node:
            self._prefs[uuid] = node
        else:
            self._prefs.pop(uuid, None)
        self._warned = {w for w in self._warned if w[0] != uuid}
        self._save()
        self._notify()

    def preferred_node(self, uuid: str, available_nodes: set[str]) -> str | None:
        """Node to try first for ``uuid`` (None = auto or node offline)."""
        node = self._prefs.get(uuid)
        if not node:
            return None
        if node in available_nodes:
            return node
        if (uuid, node) not in self._warned:
            self._warned.add((uuid, node))
            _LOGGER.warning(
                "Preferred ESP32 '%s' for shutter %s is not available; using auto",
                self._known.get(node, node),
                uuid[:8],
            )
        return None

    def as_dict(self) -> dict[str, Any]:
        return dict(self._prefs)
