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
    """Make the coordinator and the flows use plain ws:// (the fake server has no TLS)."""
    with (
        patch.object(coordinator_mod.BriaCoordinator, "_ssl_context", return_value=None),
        patch("custom_components.bria.config_flow.async_ssl_context", return_value=None),
        patch.object(machine_mod, "BACKOFF_INITIAL", 0.05),
        patch.object(machine_mod, "BACKOFF_MAX", 0.2),
    ):
        yield


def machine(name: str, *ports_or_addresses: str, machine_id: str | None = None) -> dict:
    return {"id": machine_id or name.lower(), "name": name, "addresses": list(ports_or_addresses)}


def v3_entry(port: int, *machines: dict, **kwargs):  # type: ignore[no-untyped-def]
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.bria.const import DOMAIN

    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="Bria VoIP",
        data={"port": port, "verify_ssl": True, "machines": list(machines)},
        **kwargs,
    )


async def wait_ready(entry, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    """Wait until every configured machine has finished its first status sweep."""
    import asyncio

    async with asyncio.timeout(timeout):
        while not all(m.data.get("system_product_version") for m in entry.runtime_data.machines):
            await asyncio.sleep(0.02)


@pytest.fixture
async def setup_bria(hass, fake_bria: FakeBria, point_at_fake: None):  # type: ignore[no-untyped-def]
    """Set up a v3 entry with one computer pointed at the fake server, wait until ready."""
    import asyncio

    entry = v3_entry(fake_bria.port, machine("Bria", "127.0.0.1", machine_id="main"), unique_id="test")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=5)
    await wait_ready(entry)
    await hass.async_block_till_done()
    return entry
