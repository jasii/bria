"""Smoke tests: the integration loads, connects and unloads."""
from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.bria.const import CONF_HOST, CONF_PORT, DOMAIN

from .fake_bria import FakeBria


async def test_setup_connect_unload(
    hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_bria.port},
        unique_id=f"127.0.0.1:{fake_bria.port}",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=5)

    # Poll instead of hass.async_block_till_done(): the current coordinator starts
    # its never-ending reconnect loop with hass.async_create_task, which
    # block_till_done waits on forever (audit finding R1, fixed in Phase 1).
    for _ in range(100):
        await asyncio.sleep(0.02)
        state = hass.states.get("select.bria_voip_presence_status")
        if state and state.state == "available":
            break

    assert entry.state is ConfigEntryState.LOADED
    assert "systemInformation" in fake_bria.status_types_requested()
    assert hass.states.get("binary_sensor.bria_voip_connected").state == "on"
    assert hass.states.get("sensor.bria_voip_bria_version").state == "6.5.1"
    assert hass.states.get("select.bria_voip_presence_status").state == "available"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
