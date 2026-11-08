"""Button entities: "Dimentica gruppo" for each learned YESLY group."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FinderYouCoordinator
from .group_entities import async_setup_group_entities, button_factory


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FinderYouCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_group_entities(hass, entry, coordinator, async_add_entities, button_factory)
