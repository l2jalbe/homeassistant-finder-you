"""Select entities.

- per shutter: "ESP32 preferito" (auto or the ESP32 to try first);
- per learned YESLY group: add / remove shutters by hand.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Shutter
from .const import DOMAIN
from .coordinator import FinderYouCoordinator
from .cover import shutter_device_info
from .group_entities import async_setup_group_entities, select_factory
from .preferred_nodes import AUTO, SIGNAL_PREFERRED_UPDATED

OFFLINE_SUFFIX = " (non disponibile)"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FinderYouCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PreferredNodeSelect(coordinator, sh) for sh in coordinator.shutters)
    async_setup_group_entities(hass, entry, coordinator, async_add_entities, select_factory)


class PreferredNodeSelect(CoordinatorEntity[FinderYouCoordinator], SelectEntity):
    """ESP32 node tried first for this shutter ("auto" = best signal)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "ESP32 preferito"
    _attr_icon = "mdi:access-point"

    def __init__(self, coordinator: FinderYouCoordinator, shutter: Shutter) -> None:
        super().__init__(coordinator)
        self._uuid = shutter.uuid
        self._attr_unique_id = f"{DOMAIN}_{shutter.uuid}_preferred_esp32"
        self._attr_device_info = shutter_device_info(coordinator, shutter)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PREFERRED_UPDATED, self.async_write_ha_state)
        )

    def _label_map(self) -> dict[str, str]:
        """Option label -> node id (offline nodes marked, duplicate names disambiguated)."""
        prefs = self.coordinator.preferred_nodes
        known = prefs.known_nodes()
        current = prefs.get(self._uuid)
        if current and current not in known:
            known[current] = current
        online = self.coordinator.topology_router.online_nodes()
        names = [n for n in known.values()]
        out: dict[str, str] = {}
        for node, name in sorted(known.items(), key=lambda kv: kv[1].lower()):
            label = name if names.count(name) == 1 else f"{name} ({node})"
            if node not in online:
                label += OFFLINE_SUFFIX
            out[label] = node
        return out

    @property
    def options(self) -> list[str]:
        return [AUTO, *self._label_map()]

    @property
    def current_option(self) -> str | None:
        node = self.coordinator.preferred_nodes.get(self._uuid)
        if not node:
            return AUTO
        for label, n in self._label_map().items():
            if n == node:
                return label
        return AUTO

    @property
    def extra_state_attributes(self) -> dict:
        return {"esp32_id": self.coordinator.preferred_nodes.get(self._uuid)}

    async def async_select_option(self, option: str) -> None:
        if option == AUTO:
            self.coordinator.preferred_nodes.set(self._uuid, None)
            return
        node = self._label_map().get(option)
        if node is None:
            raise ValueError(f"Unknown ESP32 option: {option}")
        self.coordinator.preferred_nodes.set(self._uuid, node)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
