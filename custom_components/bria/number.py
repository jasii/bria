"""Bria VoIP number entities — volume sliders."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GROUP_AUDIO
from .coordinator import BriaCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not entry.options.get(GROUP_AUDIO, True):
        return
    coordinator: BriaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BriaSpeakerVolumeNumber(coordinator, entry),
            BriaMicrophoneVolumeNumber(coordinator, entry),
        ]
    )


class _BriaNumberBase(CoordinatorEntity[BriaCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator: BriaCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator._host)},
            "name": "Bria VoIP",
            "manufacturer": "CounterPath",
            "model": "Bria Desktop",
        }

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class BriaSpeakerVolumeNumber(_BriaNumberBase):
    _attr_translation_key = "speaker_volume"
    _attr_icon = "mdi:volume-high"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "speaker_volume")

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.data.get("audio_speaker_volume")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_audio_properties(speaker_volume=int(value))


class BriaMicrophoneVolumeNumber(_BriaNumberBase):
    _attr_translation_key = "microphone_volume"
    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "microphone_volume")

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.data.get("audio_microphone_volume")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_audio_properties(microphone_volume=int(value))
