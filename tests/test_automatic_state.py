"""Exercise the HA coordinator with pyNukiBT and fake BLE transport."""

import asyncio
from datetime import timedelta
import importlib
import logging
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from bleak import BleakError
from construct import Container
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import update_coordinator
from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from pyNukiBT import NukiConst, NukiDevice, NukiLockConst
import pytest


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    source = Path(os.getenv("NUKI_SOURCE", Path(__file__).resolve().parents[1] / "custom_components/hass_nuki_bt"))
    package = ModuleType("nuki_under_test")
    package.__path__ = [str(source)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    mod = importlib.import_module("nuki_under_test.coordinator")
    locks = importlib.import_module("nuki_under_test.lock")
    monkeypatch.setattr(mod, "STATE_REFRESH_INTERVAL", timedelta(milliseconds=60), raising=False)
    monkeypatch.setattr(update_coordinator, "async_address_present", lambda *_: True)
    monkeypatch.setattr(update_coordinator, "async_register_callback", lambda *a, **kw: lambda: None)
    monkeypatch.setattr(update_coordinator, "async_track_unavailable", lambda *_: lambda: None)

    async def setup():
        hass = HomeAssistant(str(tmp_path))
        address = "AA:BB:CC:DD:EE:FF"
        ble = SimpleNamespace(address=address)
        device = NukiDevice(address, None, None, None, None, 1, "fixture", ble_device=ble)
        device._const = NukiLockConst
        device.config = {"ready": True}
        device.last_state = Container(config_update_count=1, lock_state=NukiLockConst.LockState.LOCKED)
        device._poll_needed = device._poll_needed_config = False
        env = SimpleNamespace(hass=hass, reads=0, publications=[], service_info=None,
                              physical=NukiLockConst.LockState.UNLOCKED, fail=False)

        async def ble_read(command, payload, *, expected_response):
            assert command == NukiConst.NukiCommand.REQUEST_DATA
            assert payload == {"command": NukiConst.NukiCommand.KEYTURNER_STATES}
            assert expected_response == NukiConst.NukiCommand.KEYTURNER_STATES
            env.reads += 1
            if env.fail:
                raise BleakError("fixture read failure")
            return Container(config_update_count=1, lock_state=env.physical)

        monkeypatch.setattr(device, "_send_encrypted_command", ble_read)
        monkeypatch.setattr(bluetooth, "async_last_service_info", lambda h, a, connectable: env.service_info)
        env.service_info = SimpleNamespace(
            device=ble, time=hass.loop.time(),
            advertisement=SimpleNamespace(manufacturer_data={76: b"\x02\xc4"}, rssi=-71),
        )
        env.coordinator = mod.NukiDataUpdateCoordinator(hass, logging.getLogger(__name__), ble, device, "fixture", "Fixture", True)
        env.coordinator._debounced_poll.cooldown = 0.005
        env.entity = LockEntity()
        env.entity.device = device
        locks.NukiLock._async_update_attrs(env.entity)

        def publish():
            locks.NukiLock._async_update_attrs(env.entity)
            env.publications.append(env.entity.state)

        env.coordinator.async_add_listener(publish)
        env.stop = env.coordinator.async_start()
        return env

    yield setup
    for name in tuple(sys.modules):
        if name.startswith(package.__name__ + "."):
            sys.modules.pop(name)


def test_recovers_without_a_change_notice_and_stops_on_unload(runtime):
    async def run():
        env = await runtime()
        try:
            assert env.entity.state == "locked"
            assert not env.coordinator.device.poll_needed()
            await asyncio.sleep(0.085)
            assert env.entity.state == "unlocked", "physical lock changed with no BLE change notice"
            assert env.reads == 1
        finally:
            env.stop()
        await asyncio.sleep(0.13)
        assert env.reads == 1

    asyncio.run(run())


def test_notification_still_updates_before_the_fallback(runtime):
    async def run():
        env = await runtime()
        try:
            env.service_info.advertisement.manufacturer_data[76] = b"\x02\xc5"
            env.coordinator._async_handle_bluetooth_event(env.service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
            await env.hass.async_block_till_done(wait_background_tasks=True)
            assert env.entity.state == "unlocked"
            assert env.reads == 1
        finally:
            env.stop()

    asyncio.run(run())


def test_read_failures_do_not_invent_state_and_recover_on_next_tick(runtime):
    async def run():
        env = await runtime()
        try:
            service_info, env.service_info = env.service_info, None
            await asyncio.sleep(0.08)
            assert env.reads == 0
            env.service_info = service_info
            env.fail = True
            await asyncio.sleep(0.062)
            assert env.reads == 1
            assert env.entity.state == "locked"
            assert not env.publications
            env.fail = False
            await asyncio.sleep(0.063)
            assert env.reads == 2
            assert env.entity.state == "unlocked"
        finally:
            env.stop()

    asyncio.run(run())
