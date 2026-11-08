"""Diagnostic sensors for the Finder YOU gateway.

These don't drive any device — they exist to help characterize when
and why the YESLY puck's cloud-to-gateway pipe goes silent. Watching
``Telemetry age`` over a few days makes the wedge pattern visible
(does it grow unbounded? plateau at a known idle level? spike right
before a verify failure?) without a packet capture.
"""

from __future__ import annotations

import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FinderYouCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FinderYouCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TelemetryAgeSensor(coordinator),
            LastCommandAgeSensor(coordinator),
            HandshakeAgeSensor(coordinator),
            ReconnectCountSensor(coordinator),
            GroupCorrectionsSensor(coordinator),
        ]
    )


def gateway_device_info(coordinator: FinderYouCoordinator) -> DeviceInfo:
    """Single 'gateway' device grouping the integration's diagnostic sensors."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"gateway_{coordinator.account_id}")},
        name="Gateway Finder YOU",
        manufacturer="Finder",
        model="YESLY gateway (cloud)",
    )


class _GatewayDiagnosticSensor(CoordinatorEntity[FinderYouCoordinator], SensorEntity):
    """Common base — both sensors share the gateway-level identity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator: FinderYouCoordinator, suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_gateway_{suffix}"
        self._attr_device_info = gateway_device_info(coordinator)


class TelemetryAgeSensor(_GatewayDiagnosticSensor):
    """Seconds since the gateway last produced any fresh per-shutter slice.

    A healthy puck sees ``0`` whenever a shutter is moved or telemetry
    refreshes; a quiet plant climbs steadily. If this stops resetting
    even while you're issuing commands the puck-to-cloud pipe is
    wedged — that's the signal we want to catch *before* a scene fails.
    """

    _attr_name = "Telemetry age"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator, "telemetry_age")

    @property
    def native_value(self) -> float | None:
        ts = self.coordinator.last_telemetry_change_ts
        if ts is None:
            return None
        return round(time.time() - ts, 1)


class LastCommandAgeSensor(_GatewayDiagnosticSensor):
    """Seconds since the integration last verified a command (motor evidence).

    Pairs with ``Telemetry age``: if commands are succeeding the gateway
    is alive; if both metrics keep climbing, the puck is idle in a way
    that may precede a wedge.
    """

    _attr_name = "Last command age"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator, "last_command_age")

    @property
    def native_value(self) -> float | None:
        ts = self.coordinator.last_successful_command_ts
        if ts is None:
            return None
        return round(time.time() - ts, 1)


class HandshakeAgeSensor(_GatewayDiagnosticSensor):
    """Seconds since the last fresh ``OpenNotificationChannel`` handshake.

    Bumps to 0 on startup, on a reactive self-heal triggered by a verify
    timeout, and on a reconnect forced by ``_run_or_reconnect`` catching
    an h2 error. Climbs monotonically the rest of the time. Useful for
    correlating "how old is the cloud-side claim?" with wedge incidents.
    """

    _attr_name = "Handshake age"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator, "handshake_age")

    @property
    def native_value(self) -> float | None:
        ts = self.coordinator.last_handshake_ts
        if ts is None:
            return None
        return round(time.time() - ts, 1)


class ReconnectCountSensor(CoordinatorEntity[FinderYouCoordinator], SensorEntity):
    """How many times the cloud session has been rebuilt since startup.

    On gateways that stay bound to the first cloud session (e.g. 1Y.GU.005)
    each reconnection can leave commands silently ignored until the gateway
    is power-cycled, so an increase here is the signal to look at.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:connection"
    _attr_name = "Cloud reconnections"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_gateway_reconnect_count"
        self._attr_device_info = gateway_device_info(coordinator)

    @property
    def native_value(self) -> int:
        return self.coordinator.reconnect_count

    @property
    def extra_state_attributes(self) -> dict:
        return {"stable_session": self.coordinator.stable_session}


class GroupCorrectionsSensor(CoordinatorEntity[FinderYouCoordinator], SensorEntity):
    """Shutters recovered after missing a button/scenario group command."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:auto-fix"
    _attr_name = "Group corrections"

    def __init__(self, coordinator: FinderYouCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_gateway_group_corrections"
        self._attr_device_info = gateway_device_info(coordinator)

    @property
    def native_value(self) -> int:
        return self.coordinator.group_supervisor.corrections_total

    @property
    def extra_state_attributes(self) -> dict:
        return {"gruppi": self.coordinator.group_supervisor.summary()}
