"""Bria switches: audio toggles and call options."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .const import GROUP_AUDIO, GROUP_CALL_OPTIONS
from .entity import BriaEntity, add_entities_for_groups

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BriaSwitchDescription(SwitchEntityDescription):
    data_key: str
    property_name: str
    group: str


def _audio(key: str, data_key: str) -> BriaSwitchDescription:
    return BriaSwitchDescription(
        key=key, translation_key=key, data_key=data_key, property_name=data_key.removeprefix("audio_"), group=GROUP_AUDIO
    )


def _option(key: str, data_key: str) -> BriaSwitchDescription:
    return BriaSwitchDescription(
        key=key,
        translation_key=key,
        data_key=data_key,
        property_name=data_key.removeprefix("call_option_"),
        group=GROUP_CALL_OPTIONS,
        entity_category=EntityCategory.CONFIG,
    )


DESCRIPTIONS: tuple[BriaSwitchDescription, ...] = (
    _audio("muted", "audio_mute"),
    _audio("speaker_muted", "audio_speaker_mute"),
    _audio("speaker_mode", "audio_speaker"),
    _option("anonymous_calling", "call_option_anonymous"),
    _option("auto_answer", "call_option_auto_answer"),
    _option("call_waiting", "call_option_call_waiting"),
    _option("letters_to_numbers", "call_option_letters_to_numbers"),
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
        SWITCH_DOMAIN,
        DESCRIPTIONS,
        lambda desc: BriaSwitch(coordinator, desc),
        async_add_entities,
    )


class BriaSwitch(BriaEntity, SwitchEntity):
    entity_description: BriaSwitchDescription

    @property
    def is_on(self) -> bool | None:
        val = self.data.get(self.entity_description.data_key)
        if val is None or isinstance(val, bool):
            return val
        return val == "enabled"

    async def _set(self, on: bool) -> None:
        value = "enabled" if on else "disabled"
        name = self.entity_description.property_name
        if self.entity_description.group == GROUP_AUDIO:
            await self.coordinator.async_set_audio_properties(**{name: value})
        else:
            await self.coordinator.async_set_call_options(**{name: value})

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
