"""Bria VoIP binary sensor entities."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    GROUP_CALL_DETAILS,
    GROUP_PHONE_INFO,
    GROUP_SCREEN_SHARE,
)
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

    # Core binary sensors — always enabled
    entities: list[BinarySensorEntity] = [
        BriaOnCallBinarySensor(coordinator, entry),
        BriaAuthenticatedBinarySensor(coordinator, entry),
        BriaConnectedBinarySensor(coordinator, entry),
    ]

    if grp(GROUP_CALL_DETAILS):
        entities.append(BriaCallOnHoldBinarySensor(coordinator, entry))

    if grp(GROUP_SCREEN_SHARE):
        entities.append(BriaScreenSharingBinarySensor(coordinator, entry))

    if grp(GROUP_PHONE_INFO):
        entities += [
            BriaPhoneReadyBinarySensor(coordinator, entry),
            BriaCanMakeCallBinarySensor(coordinator, entry),
        ]

    async_add_entities(entities)


class _BriaBinarySensorBase(CoordinatorEntity[BriaCoordinator], BinarySensorEntity):
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


class BriaOnCallBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "on_call"
    _attr_icon = "mdi:phone-in-talk"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "on_call")

    @property
    def is_on(self) -> bool:
        return len(self.coordinator.data.get("active_calls", [])) > 0


class BriaAuthenticatedBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "authenticated"
    _attr_icon = "mdi:account-check"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "authenticated")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("authenticated") is True


class BriaCallOnHoldBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "call_on_hold"
    _attr_icon = "mdi:phone-paused"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "call_on_hold")

    @property
    def is_on(self) -> bool:
        return any(c.get("hold") for c in self.coordinator.data.get("active_calls", []))


class BriaScreenSharingBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "screen_sharing"
    _attr_icon = "mdi:monitor-share"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "screen_sharing")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("screen_share_status") in ("active", "connecting")


class BriaConnectedBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "connected"
    _attr_icon = "mdi:lan-connect"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("connected", False) is True


class BriaPhoneReadyBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "phone_ready"
    _attr_icon = "mdi:phone-check"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "phone_ready")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("phone_state") == "ready"


class BriaCanMakeCallBinarySensor(_BriaBinarySensorBase):
    _attr_translation_key = "can_make_call"
    _attr_icon = "mdi:phone-plus"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "can_make_call")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("phone_call") == "allow"
