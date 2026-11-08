"""Entities for learned button/scenario groups (one device per known group).

Device "Gruppo Finder <id>" (rename it in HA to give the group a name):
- switch  "Correzione automatica"   -> enable/disable recovery for the group
- switch  "Includi <tapparella>"    -> one per learned member
- sensor  "Correzioni"              -> recoveries done for this group
- sensor  "Osservazioni"            -> learning observations (members/codes in attributes)
- button  "Dimentica gruppo"        -> drop what was learned (device removed)
- select  "Aggiungi tapparella"     -> add a shutter by hand (manual member)
- select  "Rimuovi tapparella aggiunta a mano"

Entities appear by themselves when a group becomes known or gains a member
(dispatcher signal from GroupSupervisor); no restart needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import entity_registry as er
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
        wanted = dict(factory(coordinator))
        # keys that disappeared (forgotten group, removed manual member) can be
        # added again later; their stale entities are removed from the registry
        ent_reg = er.async_get(hass)
        for key in list(added):
            if key not in wanted:
                added.discard(key)
                entity_id = ent_reg.async_get_entity_id(
                    _PLATFORM_OF.get(factory, ""), DOMAIN, f"{DOMAIN}_group_{key[0]}_{key[1]}"
                )
                if entity_id:
                    ent_reg.async_remove(entity_id)
        new: list[Entity] = []
        for key, make in wanted.items():
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


# ---- selects: manual members ---------------------------------------------

_NONE = "—"


class _GroupMemberSelect(_GroupEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_current_option = _NONE

    def _names(self) -> dict[str, str]:
        return {s.uuid: s.name.strip() for s in self.coordinator.shutters}

    def _candidates(self) -> list[str]:
        raise NotImplementedError

    @property
    def options(self) -> list[str]:
        names = self._names()
        return [_NONE] + sorted(names.get(u, u[:8]) for u in self._candidates())

    def _uuid_for(self, option: str) -> str | None:
        return next((u for u, n in self._names().items() if n == option), None)


class GroupAddMemberSelect(_GroupMemberSelect):
    """Pick a shutter to add by hand to this group."""

    _attr_name = "Aggiungi tapparella"
    _attr_icon = "mdi:plus-box-multiple"

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "add_member")

    def _candidates(self) -> list[str]:
        members = set(self._sup.members(self._gid))
        return [s.uuid for s in self.coordinator.shutters if s.uuid not in members]

    async def async_select_option(self, option: str) -> None:
        uuid = self._uuid_for(option)
        if uuid:
            self._sup.add_manual_member(self._gid, uuid)
        self.async_write_ha_state()


class GroupRemoveManualSelect(_GroupMemberSelect):
    """Pick a hand-added shutter to remove from this group."""

    _attr_name = "Rimuovi tapparella aggiunta a mano"
    _attr_icon = "mdi:minus-box-multiple"

    def __init__(self, coordinator: FinderYouCoordinator, group_id: str) -> None:
        super().__init__(coordinator, group_id, "remove_member")

    def _candidates(self) -> list[str]:
        learned = set(self._sup.learned_members(self._gid))
        return [u for u in self._sup.manual_members(self._gid) if u not in learned]

    async def async_select_option(self, option: str) -> None:
        uuid = self._uuid_for(option)
        if uuid:
            self._sup.remove_manual_member(self._gid, uuid)
        self.async_write_ha_state()


def select_factory(coordinator: FinderYouCoordinator):
    for gid in coordinator.group_supervisor.known_groups():
        yield (gid, "add_member"), (lambda g=gid: GroupAddMemberSelect(coordinator, g))
        yield (gid, "remove_member"), (lambda g=gid: GroupRemoveManualSelect(coordinator, g))


_PLATFORM_OF = {
    switch_factory: "switch",
    sensor_factory: "sensor",
    button_factory: "button",
    select_factory: "select",
}
