"""Shared fixtures for Bria tests."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import pytest

from custom_components.bria import coordinator as coordinator_mod, machine as machine_mod

from .fake_bria import FakeBria, default_status_responses


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/bria in every test."""


@pytest.fixture(autouse=True)
def no_ws_heartbeat() -> Generator[None]:
    """Disable aiohttp WebSocket heartbeats in tests (see api.BriaClient.connect)."""
    with patch("custom_components.bria.api.HEARTBEAT", None):
        yield


@pytest.fixture
async def fake_bria(socket_enabled: None) -> AsyncGenerator[FakeBria]:
    """A running fake Bria server on localhost, primed with idle-state replies."""
    fake = FakeBria(status_responses=default_status_responses())
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def point_at_fake() -> Generator[None]:
    """Make the coordinator use plain ws:// (the fake server has no TLS)."""
    with (
        patch.object(coordinator_mod.BriaCoordinator, "_ssl_context", return_value=None),
        patch.object(machine_mod, "BACKOFF_INITIAL", 0.05),
        patch.object(machine_mod, "BACKOFF_MAX", 0.2),
    ):
        yield


@pytest.fixture
async def setup_bria(hass, fake_bria: FakeBria, point_at_fake: None):  # type: ignore[no-untyped-def]
    """Set up a config entry pointed at the fake server and wait until connected."""
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.bria.const import CONF_HOST, CONF_PORT, DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Bria VoIP",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_bria.port},
        unique_id=f"127.0.0.1:{fake_bria.port}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=5)
    # Wait for the initial status sweep, not just the socket.
    async with asyncio.timeout(5):
        while entry.runtime_data.data.get("system_product_version") is None:
            await asyncio.sleep(0.02)
    await hass.async_block_till_done()
    return entry
