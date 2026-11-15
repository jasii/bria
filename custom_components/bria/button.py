"""Bria buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .entity import BriaEntity

PARALLEL_UPDATES = 0

RECONNECT = ButtonEntityDescription(
    key="reconnect", translation_key="reconnect", entity_category=EntityCategory.DIAGNOSTIC
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([BriaReconnectButton(entry.runtime_data, RECONNECT)])


class BriaReconnectButton(BriaEntity, ButtonEntity):
    """Retry every disconnected computer now instead of waiting out the backoff."""

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        self.coordinator.async_reconnect()
