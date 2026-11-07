"""Tests for the stable cloud session mode and the cloud stop fallback."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.finder_you.const import CONF_STABLE_SESSION, DOMAIN
from custom_components.finder_you.coordinator import FinderYouCoordinator

UUID = "b36d8cd3-9af0-41b0-8d50-17c8d77252b6"


def _coord(hass, stable: bool) -> FinderYouCoordinator:
    c = FinderYouCoordinator(hass, "a@b.c", "pw", stable_session=stable)
    c._plant_id = b"plant"
    c._enable_local_ble = False
    c.data = {UUID: None}
    return c


async def test_preemptive_rehandshake_disabled_in_stable(hass):
    c = _coord(hass, True)
    c._unverified_send_count = 99
    c._last_telemetry_change_ts = time.time() - 10_000
    assert c._should_preemptive_rehandshake() is False
    c2 = _coord(hass, False)
    c2._unverified_send_count = 99
    assert c2._should_preemptive_rehandshake() is True


async def test_poll_interval(hass):
    assert _coord(hass, True).update_interval.total_seconds() == 120
    assert _coord(hass, False).update_interval.total_seconds() == 30


async def test_timeout_keeps_session_in_stable(hass):
    c = _coord(hass, True)
    client = MagicMock()
    client.close = AsyncMock()
    c._client = client

    async def slow(_c):
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await c._run_or_reconnect(slow)
    assert c._client is client  # session kept
    client.close.assert_not_called()


async def test_timeout_drops_session_in_normal_mode(hass):
    c = _coord(hass, False)
    client = MagicMock()
    client.close = AsyncMock()
    c._client = client
    c._ensure_token = AsyncMock()
    calls = {"n": 0}

    async def fake_ensure():
        calls["n"] += 1
        if c._client is None:
            c._client = MagicMock()
        return c._client

    c._ensure_client = fake_ensure

    async def flaky(cl):
        if cl is client:
            raise asyncio.TimeoutError()
        return b"ok"

    assert await c._run_or_reconnect(flaky) == b"ok"
    client.close.assert_awaited()


async def test_send_command_no_inline_reclaim_in_stable(hass):
    c = _coord(hass, True)
    c._drop_client = AsyncMock()

    async def fake_send_and_verify(uuid, target, do_call, seq=None):
        c._unverified_send_count += 1  # simulate fast-path (no evidence)

    c._send_and_verify = fake_send_and_verify
    await c._send_command(UUID, 50, None)
    c._drop_client.assert_not_called()


async def test_reconnect_counter(hass):
    c = _coord(hass, True)
    c._ensure_token = AsyncMock()
    c._token = {"access_token": "x"}
    fake = MagicMock()
    fake.handshake = AsyncMock(return_value={})
    from custom_components.finder_you import coordinator as mod

    orig = mod.FinderHomeClient.connect
    mod.FinderHomeClient.connect = AsyncMock(return_value=fake)
    try:
        await c._ensure_client()
        assert c.reconnect_count == 0
        c._client = None
        await c._ensure_client()
        assert c.reconnect_count == 1
    finally:
        mod.FinderHomeClient.connect = orig


async def test_cloud_stop_sends_estimated_position(hass):
    c = _coord(hass, True)
    c._shutter_durations[UUID] = (20.0, 20.0)
    sent = {}

    async def fake_run(fn):
        cl = MagicMock()

        async def sop(plant, sid, pct):
            sent["pct"] = pct

        cl.set_open_percent = sop
        return await fn(cl)

    c._run_or_reconnect = fake_run
    now = time.time()
    # Closing from 100 -> 0, started 10.5 s ago, 20 s full travel => ~50 %
    c._shutter_start_pos[UUID] = 100
    c._pending_ble_shutter_target[UUID] = 0
    c._shutter_cmd_start_ts[UUID] = now - 10.5
    c._shutter_guard_until[UUID] = now + 30
    await c.async_stop_shutter(UUID)
    assert 45 <= sent["pct"] <= 55
    assert 45 <= c.data[UUID] <= 55


async def test_cloud_stop_unknown_start_full_close(hass):
    c = _coord(hass, True)
    c._shutter_durations[UUID] = (20.0, 20.0)
    sent = {}

    async def fake_run(fn):
        cl = MagicMock()

        async def sop(plant, sid, pct):
            sent["pct"] = pct

        cl.set_open_percent = sop
        return await fn(cl)

    c._run_or_reconnect = fake_run
    now = time.time()
    c._shutter_start_pos[UUID] = None  # unknown after HA restart
    c._pending_ble_shutter_target[UUID] = 0
    c._shutter_cmd_start_ts[UUID] = now - 5.5
    c._shutter_guard_until[UUID] = now + 30
    await c.async_stop_shutter(UUID)
    assert 70 <= sent["pct"] <= 80  # 100 -> 0, 5 s of 20 s => ~75 %


async def test_cloud_stop_unknown_position_does_nothing(hass):
    c = _coord(hass, True)
    c._run_or_reconnect = AsyncMock()
    await c.async_stop_shutter(UUID)
    c._run_or_reconnect.assert_not_called()


async def test_options_flow(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "a@b.c", "password": "x"}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_STABLE_SESSION: True}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_STABLE_SESSION] is True


async def test_diagnostic_sensors_share_gateway_device(hass):
    from custom_components.finder_you.sensor import (
        HandshakeAgeSensor,
        LastCommandAgeSensor,
        ReconnectCountSensor,
        TelemetryAgeSensor,
    )

    c = _coord(hass, True)
    sensors = [
        TelemetryAgeSensor(c),
        LastCommandAgeSensor(c),
        HandshakeAgeSensor(c),
        ReconnectCountSensor(c),
    ]
    ids = {frozenset(s.device_info["identifiers"]) for s in sensors}
    assert ids == {frozenset({(DOMAIN, "gateway_a@b.c")})}
    assert sensors[0].device_info["name"] == "Gateway Finder YOU"
    # unique_ids unchanged -> existing entity_ids are preserved
    assert [s.unique_id for s in sensors] == [
        "finder_you_gateway_telemetry_age",
        "finder_you_gateway_last_command_age",
        "finder_you_gateway_handshake_age",
        "finder_you_gateway_reconnect_count",
    ]


# --- Mesh relay status frames (real captures, 2026-11-07, shutter 05:61:06:00:F3:EB) ---

def _hx(s: str) -> list[int]:
    return [int(p, 16) for p in s.split()]


DIRECT = "05:61:06:00:F3:EB"
REPEATER = "05:61:03:20:F2:3E"
F_CLOSING = "06 00 F3 EB B1 64 10 00 41 EA 13"
F_STOP61 = "06 00 F3 EB B1 3D 00 00 42 01 13"
F_OPENING = "06 00 F3 EB B1 3D 01 00 42 12 13"
F_STOP100 = "06 00 F3 EB B1 64 00 00 42 37 13"


def _relay(frame: str) -> list[int]:
    b = _hx(frame)
    b[0] |= 0x40
    return b


def _router_with_shutter(hass):
    from custom_components.finder_you.api.plant import BleDirectDevice
    from custom_components.finder_you.topology import FinderTopologyRouter

    c = _coord(hass, True)
    c._ble_inventory = {
        UUID: BleDirectDevice(uuid=UUID, mac=DIRECT, family="13S2", key="00" * 16),
        "other": BleDirectDevice(uuid="other", mac=REPEATER, family="1YP2", key="00" * 16),
    }
    c.data = {UUID: 100}
    r = FinderTopologyRouter(hass, coordinator=c)
    return c, r


async def test_relay_frame_resolves_origin_and_updates_position(hass):
    c, r = _router_with_shutter(hass)
    # Only the RELAYED copy arrives (shutter out of direct range of the ESP32)
    r._process_finder_adv_bytes(REPEATER, -87, _relay(F_STOP61), "finder_ble_1", "finder_ble_1")
    assert c.data[UUID] == 61
    assert c.shutter_motion(UUID) is None


async def test_relay_and_direct_copies_are_deduplicated(hass):
    c, r = _router_with_shutter(hass)
    calls = []
    orig = c.handle_ble_device_state_update
    c.handle_ble_device_state_update = lambda *a, **k: (calls.append(k), orig(*a, **k))
    r._process_finder_adv_bytes(DIRECT, -69, _hx(F_STOP61), "n", "n")
    r._process_finder_adv_bytes(REPEATER, -87, _relay(F_STOP61), "n", "n")
    assert len(calls) == 1
    assert c.data[UUID] == 61


async def test_motion_flags_from_relay(hass):
    c, r = _router_with_shutter(hass)
    r._process_finder_adv_bytes(REPEATER, -79, _relay(F_CLOSING), "n", "n")
    assert c.shutter_motion(UUID) == "closing"
    r._process_finder_adv_bytes(REPEATER, -87, _relay(F_STOP61), "n", "n")
    assert c.shutter_motion(UUID) is None
    r._process_finder_adv_bytes(REPEATER, -81, _relay(F_OPENING), "n", "n")
    assert c.shutter_motion(UUID) == "opening"
    r._process_finder_adv_bytes(REPEATER, -88, _relay(F_STOP100), "n", "n")
    assert c.shutter_motion(UUID) is None
    assert c.data[UUID] == 100


async def test_unknown_relay_origin_still_ignored(hass):
    c, r = _router_with_shutter(hass)
    unknown = _relay("01 02 03 04 B1 10 00 00 42 01 13")
    r._process_finder_adv_bytes(REPEATER, -80, unknown, "n", "n")
    assert c.data[UUID] == 100


async def test_relayed_command_frame_ignored(hass):
    c, r = _router_with_shutter(hass)
    # 9-byte relayed frame (command) for the same origin -> not telemetry
    r._process_finder_adv_bytes(REPEATER, -80, _relay("06 00 F3 EB 05 AA BB CC DD"), "n", "n")
    assert c.data[UUID] == 100


async def test_motion_expires(hass):
    c, _ = _router_with_shutter(hass)
    c._shutter_durations[UUID] = (20.0, 20.0)
    c._shutter_motion[UUID] = ("closing", time.time() - 31)
    assert c.shutter_motion(UUID) is None
