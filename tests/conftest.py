"""Shared fixtures for Bria tests."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import pytest

from custom_components.bria import coordinator as coordinator_mod

from .fake_bria import FakeBria, default_status_responses


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/bria in every test."""


@pytest.fixture
async def fake_bria(socket_enabled: None) -> AsyncGenerator[FakeBria]:
    """A running fake Bria server on localhost, primed with idle-state replies."""
    fake = FakeBria(status_responses=default_status_responses())
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def point_at_fake(fake_bria: FakeBria) -> Generator[None]:
    """Make the coordinator connect to the fake server over plain ws://.

    Phase 0 shim: the current coordinator hard-codes wss://cpclientapi... in
    __init__. Phase 1 replaces this with a real address/scheme seam.
    """
    original_init = coordinator_mod.BriaCoordinator.__init__

    def _init(self, hass, host, port):  # type: ignore[no-untyped-def]
        original_init(self, hass, host, port)
        self._ws_url = fake_bria.url

    with patch.object(coordinator_mod.BriaCoordinator, "__init__", _init):
        yield
