"""Bria VoIP switch entities — toggleable controls."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GROUP_AUDIO, GROUP_CALL_OPTIONS
from .coordinator import BriaCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BriaCoordinator = hass.data[DOMAIN][entry.entry_id]
    opts = entry.options

    def grp(key: str) -> bool:
        return opts.get(key, True)

    entities: list[SwitchEntity] = []

    if grp(GROUP_AUDIO):
        entities += [
            BriaMuteSwitch(coordinator, entry),
            BriaSpeakerMuteSwitch(coordinator, entry),
            BriaSpeakerModeSwitch(coordinator, entry),
        ]

    if grp(GROUP_CALL_OPTIONS):
        entities += [
            BriaAnonymousCallingSwitch(coordinator, entry),
            BriaAutoAnswerSwitch(coordinator, entry),
            BriaCallWaitingSwitch(coordinator, entry),
            BriaLettersToNumbersSwitch(coordinator, entry),
        ]

    async_add_entities(entities)


class _BriaSwitchBase(CoordinatorEntity[BriaCoordinator], SwitchEntity):
    _attr_has_entity_name = True

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


class BriaMuteSwitch(_BriaSwitchBase):
    _attr_translation_key = "muted"
    _attr_icon = "mdi:microphone-off"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "muted")

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.data.get("audio_mute")
        return None if val is None else val == "enabled"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(mute="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(mute="disabled")


class BriaSpeakerMuteSwitch(_BriaSwitchBase):
    _attr_translation_key = "speaker_muted"
    _attr_icon = "mdi:volume-off"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "speaker_muted")

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.data.get("audio_speaker_mute")
        return None if val is None else val == "enabled"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(speaker_mute="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(speaker_mute="disabled")


class BriaSpeakerModeSwitch(_BriaSwitchBase):
    _attr_translation_key = "speaker_mode"
    _attr_icon = "mdi:volume-high"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "speaker_mode")

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.data.get("audio_speaker")
        return None if val is None else val == "enabled"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(speaker="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_audio_properties(speaker="disabled")


class BriaAnonymousCallingSwitch(_BriaSwitchBase):
    _attr_translation_key = "anonymous_calling"
    _attr_icon = "mdi:incognito"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "anonymous_calling")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("call_option_anonymous")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(anonymous="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(anonymous="disabled")


class BriaAutoAnswerSwitch(_BriaSwitchBase):
    _attr_translation_key = "auto_answer"
    _attr_icon = "mdi:phone-auto"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "auto_answer")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("call_option_auto_answer")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(auto_answer="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(auto_answer="disabled")


class BriaCallWaitingSwitch(_BriaSwitchBase):
    _attr_translation_key = "call_waiting"
    _attr_icon = "mdi:phone-ring"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "call_waiting")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("call_option_call_waiting")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(call_waiting="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(call_waiting="disabled")


class BriaLettersToNumbersSwitch(_BriaSwitchBase):
    _attr_translation_key = "letters_to_numbers"
    _attr_icon = "mdi:numeric"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "letters_to_numbers")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("call_option_letters_to_numbers")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(letters_to_numbers="enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_call_options(letters_to_numbers="disabled")
