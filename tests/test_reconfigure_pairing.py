"""Verify in-place re-pairing without using a live lock."""

import asyncio
from collections.abc import Mapping
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bleak import BleakError
from homeassistant.config_entries import ConfigEntryState
import pytest


@pytest.mark.parametrize("action", ["bridge", "failed", "unchanged"])
def test_reconfigure_preserves_entry_identity(monkeypatch, action):
    """Only successful pairing switches credentials, and never creates an entry."""
    package = ModuleType("nuki_pairing_test")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/hass_nuki_bt")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    config = importlib.import_module(package.__name__ + ".config_flow")

    async def run():
        entry = SimpleNamespace(
            entry_id="building-entry", unique_id="building-address", state=ConfigEntryState.LOADED,
            data={"name": "Building Entrance", "device_address": "AA:BB:CC:DD:EE:FF",
                  "client_type": "App", "auth_id": "old"}, options={"other": True},
        )
        flow = config.NukiFlowHandler()
        flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_unload=AsyncMock(return_value=True), async_schedule_reload=Mock(),
        ))
        flow._get_reconfigure_entry = Mock(return_value=entry)
        flow.async_update_reload_and_abort = Mock(return_value="reconfigured")
        flow.async_update_and_abort = Mock(return_value="unchanged")
        flow.async_show_form = Mock(side_effect=lambda **data: data)
        flow._async_pair = AsyncMock(return_value={"auth_id": "new"})
        view = await flow.async_step_reconfigure()
        assert isinstance(view, Mapping) and not flow._async_pair.called
        if action == "failed":
            flow._async_pair.side_effect = BleakError("fixture connection failure")
        result = await flow.async_step_reconfigure({
            "client_type": "App" if action == "unchanged" else "Bridge",
        })
        if action == "unchanged":
            assert result == "unchanged"
            flow.async_update_and_abort.assert_called_once_with(entry)
            assert not flow._async_pair.called
            assert not flow.hass.config_entries.async_unload.called
        else:
            flow.hass.config_entries.async_unload.assert_awaited_once_with(entry.entry_id)
            assert flow._async_pair.await_args.args[0]["client_type"] == "Bridge"
            assert flow._async_pair.await_args.args[0]["device_address"] == entry.data["device_address"]
            if action == "failed":
                assert result["errors"] == {"base": "connection"}
                assert not flow.async_update_reload_and_abort.called
                flow.hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)
            else:
                assert result == "reconfigured"
                flow.async_update_reload_and_abort.assert_called_once_with(
                    entry, data_updates={"auth_id": "new", "client_type": "Bridge"},
                )

    try:
        asyncio.run(run())
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package.__name__ + "."):
                sys.modules.pop(name)
