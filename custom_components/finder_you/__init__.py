"""Finder YOU custom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_STABLE_SESSION,
    DEFAULT_STABLE_SESSION,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import FinderYouCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = FinderYouCoordinator(
        hass,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        stable_session=entry.options.get(CONF_STABLE_SESSION, DEFAULT_STABLE_SESSION),
    )
    await coordinator.group_supervisor.async_load()
    await coordinator.preferred_nodes.async_load()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_stale_devices(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_remove_stale_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove devices of this entry left without entities.

    Up to v0.2.0 every scenario was its own device ("scenario_<uuid>"); since
    v0.3.0 they share the "Scenari Finder YOU" device, so the old ones are
    empty. Only empty devices whose identifier starts with "scenario_" are
    touched.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not any(dom == DOMAIN and str(ident).startswith("scenario_") for dom, ident in device.identifiers):
            continue
        if er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
            continue
        dev_reg.async_remove_device(device.id)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so a changed option takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: FinderYouCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
