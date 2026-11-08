"""Entities for learned button/scenario groups (one device per known group).

Device "Gruppo Finder <id>" (rename it in HA to give the group a name):
- switch  "Correzione automatica"   -> enable/disable recovery for the group
- switch  "Includi <tapparella>"    -> one per learned member
- sensor  "Correzioni"              -> recoveries done for this group
- sensor  "Osservazioni"            -> learning observations (members/codes in attributes)
- button  "Dimentica gruppo"        -> drop what was learned (device removed)

Entities appear by themselves when a group becomes known or gains a member
(dispatcher signal from GroupSupervisor); no restart needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FinderYouCoordinator
from .group_supervisor import SIGNAL_GROUPS_UPDATED, GroupSupervisor

Factory = Callable[[FinderYouCoordinator], Iterable[tuple[tuple[str, str], Callable[[], Entity]]]]


def group_device_info(group_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"group_{group_id}")},
        name=f"Gruppo Finder {group_id}",
        manufacturer="Finder",
        model="Gruppo pulsante/scenario YESLY",
    )


@callback
def async_setup_group_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: FinderYouCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Factory,
) -> None:
    """Add group entities now and whenever the learned groups change."""
    added: set[tuple[str, str]] = set()

    @callback
    def _sync() -> None:
        known = set(coordinator.group_supervisor.known_groups())
        for key in list(added):  # forgotten groups can be re-added if re-learned
            if key[0] not in known:
                added.discard(key)
        new: list[Entity] = []
        for key, make in factory(coordinator):
            if key not in added:
                added.add(key)
                new.append(make())
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_GROUPS_UPDATED, _sync))


class _GroupEntity(CoordinatorEntity[FinderYouCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str, suffix: str) -> None:
        super().__init__(coordinator)
        self._gid = group_id
        self._attr_unique_id = f"{DOMAIN}_group_{group_id}_{suffix}"
        self._attr_device_info = group_device_info(group_id)

    @property
    def _sup(self) -> GroupSupervisor:
        return self.coordinator.group_supervisor

    @property
    def available(self) -> bool:
        return self._gid in self._sup.known_groups()


# ---- switches --------------------------------------------------------------


class GroupCorrectionSwitch(_GroupEntity, SwitchEntity):
    _attr_name = "Correzione automatica"
    _attr_icon = "mdi:auto-fix"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "correction")

    @property
    def is_on(self) -> bool:
        return self._sup.correction_enabled(self._gid)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._sup.set_correction_enabled(self._gid, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._sup.set_correction_enabled(self._gid, False)
        self.async_write_ha_state()


class GroupMemberSwitch(_GroupEntity, SwitchEntity):
    _attr_icon = "mdi:window-shutter-settings"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str, uuid: str, name: str) -> None:
        super().__init__(coordinator, group_id, f"member_{uuid}")
        self._uuid = uuid
        self._attr_name = f"Includi {name}"

    @property
    def available(self) -> bool:
        return super().available and self._uuid in self._sup.members(self._gid)

    @property
    def is_on(self) -> bool:
        return self._sup.member_included(self._gid, self._uuid)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._sup.set_member_included(self._gid, self._uuid, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._sup.set_member_included(self._gid, self._uuid, False)
        self.async_write_ha_state()


def switch_factory(coordinator: FinderYouCoordinator):
    sup = coordinator.group_supervisor
    names = {s.uuid: s.name.strip() for s in coordinator.shutters}
    for gid in sup.known_groups():
        yield (gid, "correction"), (lambda g=gid: GroupCorrectionSwitch(coordinator, g))
        for uuid in sup.members(gid):
            yield (gid, f"member_{uuid}"), (
                lambda g=gid, u=uuid: GroupMemberSwitch(coordinator, g, u, names.get(u, u[:8]))
            )


# ---- sensors ---------------------------------------------------------------


class GroupCorrectionsCountSensor(_GroupEntity, SensorEntity):
    _attr_name = "Correzioni"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "corrections")

    @property
    def native_value(self) -> int:
        return self._sup.corrections(self._gid)


class GroupObservationsSensor(_GroupEntity, SensorEntity):
    _attr_name = "Osservazioni"
    _attr_icon = "mdi:eye-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "observations")

    @property
    def native_value(self) -> int:
        return self._sup.summary().get(self._gid, {}).get("osservazioni", 0)

    @property
    def extra_state_attributes(self) -> dict:
        info = self._sup.summary().get(self._gid, {})
        return {
            "codice_gruppo": self._gid,
            "membri": info.get("membri", []),
            "codici": info.get("codici", {}),
        }


def sensor_factory(coordinator: FinderYouCoordinator):
    for gid in coordinator.group_supervisor.known_groups():
        yield (gid, "corrections"), (lambda g=gid: GroupCorrectionsCountSensor(coordinator, g))
        yield (gid, "observations"), (lambda g=gid: GroupObservationsSensor(coordinator, g))


# ---- button ----------------------------------------------------------------


class ForgetGroupButton(_GroupEntity, ButtonEntity):
    _attr_name = "Dimentica gruppo"
    _attr_icon = "mdi:delete-restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "forget")

    async def async_press(self) -> None:
        self._sup.forget(self._gid)


def button_factory(coordinator: FinderYouCoordinator):
    for gid in coordinator.group_supervisor.known_groups():
        yield (gid, "forget"), (lambda g=gid: ForgetGroupButton(coordinator, g))
