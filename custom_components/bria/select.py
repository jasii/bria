"""Bria VoIP select entity — presence status picker."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GROUP_PRESENCE, PRESENCE_OPTIONS
from .coordinator import BriaCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not entry.options.get(GROUP_PRESENCE, True):
        return
    coordinator: BriaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BriaPresenceSelect(coordinator, entry)])


class BriaPresenceSelect(CoordinatorEntity[BriaCoordinator], SelectEntity):
    """Lets the user view and change the Bria presence status from the HA UI."""

    _attr_has_entity_name = True
    _attr_translation_key = "presence_status"
    _attr_icon = "mdi:account-circle"
    _attr_options = PRESENCE_OPTIONS

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_presence_status"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator._host)},
            "name": "Bria VoIP",
            "manufacturer": "CounterPath",
            "model": "Bria Desktop",
        }

    @property
    def current_option(self) -> str | None:
        status = self.coordinator.data.get("presence_status")
        return status if status in PRESENCE_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_presence(option)
        # Optimistic update so the UI reflects the change immediately
        self.coordinator.data["presence_status"] = option
        self.coordinator.data["presence_text"] = ""
        self.async_write_ha_state()
