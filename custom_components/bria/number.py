"""Bria volume sliders."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .const import GROUP_AUDIO
from .entity import BriaEntity, add_entities_for_groups

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BriaNumberDescription(NumberEntityDescription):
    data_key: str
    property_name: str
    group: str | None = GROUP_AUDIO


DESCRIPTIONS: tuple[BriaNumberDescription, ...] = (
    BriaNumberDescription(
        key="speaker_volume",
        translation_key="speaker_volume",
        data_key="audio_speaker_volume",
        property_name="speaker_volume",
    ),
    BriaNumberDescription(
        key="microphone_volume",
        translation_key="microphone_volume",
        data_key="audio_microphone_volume",
        property_name="microphone_volume",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_entities_for_groups(
        hass,
        entry,
        NUMBER_DOMAIN,
        DESCRIPTIONS,
        lambda desc: BriaVolumeNumber(coordinator, desc),
        async_add_entities,
    )


class BriaVolumeNumber(BriaEntity, NumberEntity):
    entity_description: BriaNumberDescription
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        val = self.data.get(self.entity_description.data_key)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_audio_properties(
            **{self.entity_description.property_name: int(value)}
        )
