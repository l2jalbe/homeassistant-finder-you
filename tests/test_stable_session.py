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
