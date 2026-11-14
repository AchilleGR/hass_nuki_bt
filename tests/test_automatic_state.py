"""Exercise the HA coordinator with pyNukiBT and fake BLE transport."""

import asyncio
import importlib
import logging
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

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
        env = SimpleNamespace(hass=hass, reads=0, service_info=None)

        async def ble_read(command, payload, *, expected_response):
            assert command == NukiConst.NukiCommand.REQUEST_DATA
            assert payload == {"command": NukiConst.NukiCommand.KEYTURNER_STATES}
            assert expected_response == NukiConst.NukiCommand.KEYTURNER_STATES
            env.reads += 1
            return Container(config_update_count=1, lock_state=NukiLockConst.LockState.UNLOCKED)

        monkeypatch.setattr(device, "_send_encrypted_command", ble_read)
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

        env.coordinator.async_add_listener(publish)
        env.stop = env.coordinator.async_start()
        return env

    yield setup
    for name in tuple(sys.modules):
        if name.startswith(package.__name__ + "."):
            sys.modules.pop(name)


def test_notification_updates_lock_state(runtime):
    """A normal BLE advertisement does not read state; a change notice does."""
    async def run():
        env = await runtime()
        try:
            assert env.entity.state == "locked"
            env.coordinator._async_handle_bluetooth_event(env.service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
            await env.hass.async_block_till_done(wait_background_tasks=True)
            assert env.reads == 0
            env.service_info.advertisement.manufacturer_data[76] = b"\x02\xc5"
            env.coordinator._async_handle_bluetooth_event(env.service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
            await env.hass.async_block_till_done(wait_background_tasks=True)
            assert env.entity.state == "unlocked"
            assert env.reads == 1
        finally:
            env.stop()

    asyncio.run(run())
