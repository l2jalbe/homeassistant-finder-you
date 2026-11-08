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


# --- Routing map (/config/finder_you_mappa_personalizzata.yaml) ---------------

from custom_components.finder_you.routing_map import CustomRoutingMap  # noqa: E402

SHUTTERS = [("Cucina finestra sx ", "b36d8cd3-aaaa"), ("Lavanderia", "31fff8d8-bbbb"), ("1P Bagno ", "f68df89d-cccc")]


def test_routing_map_created_with_all_shutters_auto(tmp_path):
    p = tmp_path / "finder_you_mappa_personalizzata.yaml"
    m = CustomRoutingMap(str(p))
    m.sync(SHUTTERS, ["finder_ble_1"])
    text = p.read_text()
    assert "COME SI COMPILA" in text
    assert '"Cucina finestra sx": auto' in text
    assert '"Lavanderia": auto' in text
    assert '"1P Bagno": auto' in text
    assert "# ESP32 rilevati: finder_ble_1" in text
    assert m.preferred_node("b36d8cd3-aaaa", {"finder_ble_1"}) is None


def test_routing_map_user_value_used_and_preserved(tmp_path):
    p = tmp_path / "m.yaml"
    m = CustomRoutingMap(str(p))
    m.sync(SHUTTERS, ["finder_ble_1", "finder_ble_2"])
    p.write_text(p.read_text().replace('"Cucina finestra sx": auto', '"Cucina finestra sx": finder-ble-2'))
    import os, time as _t
    os.utime(p, (_t.time() + 5, _t.time() + 5))
    m.sync(SHUTTERS, ["finder_ble_1", "finder_ble_2"])
    assert m.preferred_node("b36d8cd3-aaaa", {"finder_ble_1", "finder_ble_2"}) == "finder_ble_2"
    assert m.preferred_node("31fff8d8-bbbb", {"finder_ble_1", "finder_ble_2"}) is None
    # new shutter + new node -> file rewritten, user value kept
    m.sync(SHUTTERS + [("Sala finestra dx", "72a6279a-dddd")], ["finder_ble_1", "finder_ble_2", "finder_ble_3"])
    text = p.read_text()
    assert '"Cucina finestra sx": finder-ble-2' in text
    assert '"Sala finestra dx": auto' in text
    assert "finder_ble_3" in text


def test_routing_map_offline_or_unknown_node_falls_back_to_auto(tmp_path):
    p = tmp_path / "m.yaml"
    m = CustomRoutingMap(str(p))
    m.sync(SHUTTERS, ["finder_ble_1"])
    p.write_text(p.read_text().replace('"Lavanderia": auto', '"Lavanderia": esp_inesistente'))
    import os, time as _t
    os.utime(p, (_t.time() + 5, _t.time() + 5))
    m.sync(SHUTTERS, ["finder_ble_1"])
    assert m.preferred_node("31fff8d8-bbbb", {"finder_ble_1"}) is None


def test_routing_map_syntax_error_keeps_previous_values(tmp_path):
    p = tmp_path / "m.yaml"
    m = CustomRoutingMap(str(p))
    m.sync(SHUTTERS, ["finder_ble_1", "finder_ble_2"])
    p.write_text(p.read_text().replace('"Lavanderia": auto', '"Lavanderia": finder_ble_2'))
    import os, time as _t
    os.utime(p, (_t.time() + 5, _t.time() + 5))
    m.sync(SHUTTERS, ["finder_ble_1", "finder_ble_2"])
    broken = 'tapparelle:\n  "Lavanderia": [unclosed\n'
    p.write_text(broken)
    os.utime(p, (_t.time() + 10, _t.time() + 10))
    m.sync(SHUTTERS, ["finder_ble_1", "finder_ble_2"])
    assert p.read_text() == broken  # never overwrite a file being edited
    assert m.preferred_node("31fff8d8-bbbb", {"finder_ble_1", "finder_ble_2"}) == "finder_ble_2"


async def test_select_best_node_uses_routing_map_then_rssi(hass, tmp_path):
    c, r = _router_with_shutter(hass)
    r._esphome_nodes = {"finder_ble_1", "finder_ble_2"}
    r._discover_registered_nodes = lambda: None
    dev = c._ble_inventory[UUID]
    now = time.time()
    r._topology[DIRECT] = {
        "finder_ble_1": {"rssi": -65, "last_seen": now},
        "finder_ble_2": {"rssi": -80, "last_seen": now},
    }
    c.routing_map = CustomRoutingMap(str(tmp_path / "m.yaml"))
    c.routing_map.sync([("Cucina finestra sx", UUID)], ["finder_ble_1", "finder_ble_2"])
    assert r.select_best_node(dev) == "finder_ble_1"  # auto -> best RSSI
    assert r.select_alternate_node(dev, exclude_node="finder_ble_1") == "finder_ble_2"
    c.routing_map._by_name["Cucina finestra sx"] = "finder_ble_2"
    assert r.select_best_node(dev) == "finder_ble_2"  # forced
    assert r.select_alternate_node(dev, exclude_node="finder_ble_2") == "finder_ble_1"
    r._esphome_nodes = {"finder_ble_1"}  # forced node offline -> auto
    assert r.select_best_node(dev) == "finder_ble_1"


async def test_no_author_specific_rules_left(hass):
    from custom_components.finder_you import topology

    src = open(topology.__file__).read()
    for token in ("bluetooth_proxy_sara", "esphome_web_cef3a0", "18264c", "NODE_ADJACENCY_MAP", "SOURCE_MAC_TO_NODE_MAP", "_get_affinity_node"):
        assert token not in src


# --- Group supervisor (strada B) - based on real captures 2026-11-08 ---------

from datetime import timedelta  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402

from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: E402

from custom_components.finder_you.api.plant import Shutter  # noqa: E402

SALA_DX = "72a6279a-3ea3-418e-90d0-e821374c3fae"
SALA_SX = "1bd5ec9e-90d2-4d8d-abb3-498aedcefffc"
INGRESSO = "7c491637-4a7a-45f1-8528-33c4f550e373"
CMD_OPEN = "45 0B 00 00 09 59 1E A1 C7 E2 15 00 00 8A 17"   # seq 5, code 09 (apri)
CMD_CLOSE = "49 0B 00 00 11 83 60 AC 54 E2 15 00 00 8A 17"  # seq 9, code 11 (chiudi)


def _cmd(frame: str, seq: int) -> list[int]:
    b = _hx(frame)
    b[0] = 0x40 | seq
    return b


def _shutter(uuid, name):
    import dataclasses
    fields = {f.name for f in dataclasses.fields(Shutter)}
    kw = {"uuid": uuid, "name": name}
    for f in fields - kw.keys():
        kw[f] = None
    return Shutter(**kw)


def _sup_coord(hass, tmp_path):
    c = _coord(hass, True)
    c._shutters = [_shutter(SALA_DX, "Sala finestra dx"), _shutter(SALA_SX, "Sala finestra sx"),
                   _shutter(INGRESSO, "Portafinestra ingresso")]
    c.data = {SALA_DX: 100, SALA_SX: 100, INGRESSO: 100}
    c.routing_map = CustomRoutingMap(str(tmp_path / "m.yaml"))
    c.async_set_position = AsyncMock()
    return c, c.group_supervisor


async def _press(hass, sup, frame, seq, movers, direction):
    # real presses are seconds apart: forget motion from the previous press
    sup._motion_start = {u: (ts - 60, d) for u, (ts, d) in sup._motion_start.items()}
    sup._motion_now = {}
    sup.handle_command_frame(_cmd(frame, seq))
    sup.handle_command_frame(_cmd(frame, seq))  # duplicate copy from 2nd gateway
    t0 = sup._active["0B0000"].ts
    for u in movers:
        sup._motion_start[u] = (t0 + 1.0, direction)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()


async def _teach(hass, sup):
    seq = 1
    for _ in range(2):
        await _press(hass, sup, CMD_CLOSE, seq, [SALA_DX, SALA_SX, INGRESSO], "close"); seq += 1
        await _press(hass, sup, CMD_OPEN, seq, [SALA_DX, SALA_SX, INGRESSO], "open"); seq += 1
    return seq


async def test_group_learning_from_motion(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    await _teach(hass, sup)
    assert sup.is_known("0B0000")
    assert set(sup.members("0B0000")) == {SALA_DX, SALA_SX, INGRESSO}
    assert sup.code_direction("0B0000", 0x09) == "open"
    assert sup.code_direction("0B0000", 0x11) == "close"
    c.async_set_position.assert_not_called()
    s = sup.summary()["0B0000"]
    assert s["membri"] == ["Portafinestra ingresso", "Sala finestra dx", "Sala finestra sx"]
    assert s["codici"] == {"09": "apri", "11": "chiudi"}


async def test_group_correction_of_missing_member(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 0, SALA_SX: 0, INGRESSO: 0}
    # the real case: 'apri' at mid-course, Sala finestra sx does not react
    await _press(hass, sup, CMD_OPEN, seq, [SALA_DX, INGRESSO], "open")
    c.async_set_position.assert_awaited_once_with(SALA_SX, 100)
    assert sup.corrections_total == 1


async def test_no_correction_when_nobody_moved(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 100, SALA_SX: 100, INGRESSO: 100}
    await _press(hass, sup, CMD_OPEN, seq, [], "open")  # 'apri' with all already open
    c.async_set_position.assert_not_called()


async def test_no_correction_when_already_at_target(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 0, SALA_SX: 100, INGRESSO: 0}
    await _press(hass, sup, CMD_OPEN, seq, [SALA_DX, INGRESSO], "open")
    c.async_set_position.assert_not_called()


async def test_unknown_code_never_corrected(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 50, SALA_SX: 50, INGRESSO: 50}
    unknown = CMD_OPEN.replace(" 09 ", " 08 ", 1)
    await _press(hass, sup, unknown, seq, [SALA_DX, INGRESSO], "open")
    c.async_set_position.assert_not_called()


async def test_superseded_command_not_corrected(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 0, SALA_SX: 0, INGRESSO: 0}
    sup._motion_start = {}
    sup.handle_command_frame(_cmd(CMD_OPEN, seq))
    t0 = sup._active["0B0000"].ts
    sup._motion_start[SALA_DX] = (t0 + 0.8, "open")
    sup.handle_command_frame(_cmd(CMD_CLOSE, seq + 1))  # user presses again before 2.5 s
    sup._motion_start[SALA_DX] = (t0 + 1.2, "close")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    calls = [a.args for a in c.async_set_position.await_args_list]
    assert (SALA_SX, 100) not in calls  # the superseded 'apri' is never applied


async def test_correction_disabled_or_excluded_in_map(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.routing_map._group_raw = {"0B0000": {"correzione": "no"}}
    c.data = {SALA_DX: 0, SALA_SX: 0, INGRESSO: 0}
    await _press(hass, sup, CMD_OPEN, seq, [SALA_DX, INGRESSO], "open")
    c.async_set_position.assert_not_called()
    c.routing_map._group_raw = {"0B0000": {"correzione": "si", "esclusi": ["Sala finestra sx"]}}
    c.data = {SALA_DX: 0, SALA_SX: 0, INGRESSO: 0}
    await _press(hass, sup, CMD_OPEN, seq + 1, [SALA_DX, INGRESSO], "open")
    c.async_set_position.assert_not_called()


async def test_ha_commanded_shutter_not_learned_nor_corrected(hass, tmp_path):
    c, sup = _sup_coord(hass, tmp_path)
    sup.note_ha_command(SALA_SX)
    await _press(hass, sup, CMD_CLOSE, 1, [SALA_DX, SALA_SX], "close")
    assert SALA_SX not in sup._groups["0B0000"]["members"]


def test_routing_map_groups_section_roundtrip(tmp_path):
    p = tmp_path / "m.yaml"
    m = CustomRoutingMap(str(p))
    groups = {"0B0000": {"membri": ["Sala finestra dx", "Sala finestra sx"], "codici": {"09": "apri", "11": "chiudi"}, "osservazioni": 4}}
    m.sync(SHUTTERS, ["finder_ble_1"], groups)
    text = p.read_text()
    assert 'gruppi:' in text and '"0B0000":' in text and 'correzione: si' in text
    assert 'codici: {"09": apri, "11": chiudi}' in text
    text = text.replace('nome: ""', 'nome: "Pulsante sala"').replace("correzione: si", "correzione: no")
    p.write_text(text)
    import os, time as _t
    os.utime(p, (_t.time() + 5, _t.time() + 5))
    m.sync(SHUTTERS, ["finder_ble_1"], groups)
    st = m.group_settings("0B0000")
    assert st["enabled"] is False and st["name"] == "Pulsante sala"
    groups["0B0000"]["osservazioni"] = 5   # learning progresses -> file rewritten, user values kept
    m.sync(SHUTTERS, ["finder_ble_1"], groups)
    text = p.read_text()
    assert 'nome: "Pulsante sala"' in text and "correzione: no" in text and "osservazioni: 5" in text


async def test_topology_forwards_command_frames(hass, tmp_path):
    c, r = _router_with_shutter(hass)
    c.group_supervisor.handle_command_frame = MagicMock()
    r._process_finder_adv_bytes(REPEATER, -90, _cmd(CMD_OPEN, 5), "n", "n")
    c.group_supervisor.handle_command_frame.assert_called_once()


async def test_member_still_moving_old_way_is_corrected(hass, tmp_path):
    """Real case: Sala sx was closing, missed 'apri' and kept closing."""
    c, sup = _sup_coord(hass, tmp_path)
    seq = await _teach(hass, sup)
    c.data = {SALA_DX: 60, SALA_SX: 60, INGRESSO: 60}
    sup._motion_start = {}
    sup._motion_now = {SALA_DX: "close", SALA_SX: "close", INGRESSO: "close"}
    sup.handle_command_frame(_cmd(CMD_OPEN, seq))
    # status frames after the command: two reverse, Sala sx keeps reporting "closing"
    sup.note_motion(SALA_DX, "opening")
    sup.note_motion(INGRESSO, "opening")
    sup.note_motion(SALA_SX, "closing")
    t0 = sup._active["0B0000"].ts
    for u in (SALA_DX, INGRESSO):
        sup._motion_start[u] = (t0 + 1.0, "open")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    c.async_set_position.assert_awaited_once_with(SALA_SX, 100)


def test_note_motion_records_only_starts(hass):
    from custom_components.finder_you.group_supervisor import GroupSupervisor
    sup = GroupSupervisor(hass, MagicMock())
    sup.note_motion("u", "closing")
    t1 = sup._motion_start["u"][0]
    sup.note_motion("u", "closing")
    assert sup._motion_start["u"][0] == t1
    sup.note_motion("u", None)
    sup.note_motion("u", "closing")
    assert sup._motion_start["u"][0] >= t1
