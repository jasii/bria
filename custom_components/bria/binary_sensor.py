"""Bria binary sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .const import GROUP_CALL_DETAILS, GROUP_PHONE_INFO, GROUP_SCREEN_SHARE
from .entity import BriaEntity, BriaMachineEntity, add_entities_for_groups

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BriaBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    group: str | None = None
    always_available: bool = False


DESCRIPTIONS: tuple[BriaBinarySensorDescription, ...] = (
    BriaBinarySensorDescription(
        key="on_call",
        translation_key="on_call",
        value_fn=lambda d: bool(d.get("active_calls")),
    ),
    BriaBinarySensorDescription(
        key="authenticated",
        translation_key="authenticated",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("authenticated") is True,
    ),
    BriaBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_available=True,
        value_fn=lambda d: d.get("connected") is True,
    ),
    BriaBinarySensorDescription(
        key="call_on_hold",
        translation_key="call_on_hold",
        group=GROUP_CALL_DETAILS,
        value_fn=lambda d: any(c.get("hold") for c in d.get("active_calls", [])),
    ),
    BriaBinarySensorDescription(
        key="screen_sharing",
        translation_key="screen_sharing",
        device_class=BinarySensorDeviceClass.RUNNING,
        group=GROUP_SCREEN_SHARE,
        value_fn=lambda d: d.get("screen_share_status") in ("active", "connecting"),
    ),
    BriaBinarySensorDescription(
        key="phone_ready",
        translation_key="phone_ready",
        group=GROUP_PHONE_INFO,
        value_fn=lambda d: d.get("phone_state") == "ready",
    ),
    BriaBinarySensorDescription(
        key="can_make_call",
        translation_key="can_make_call",
        group=GROUP_PHONE_INFO,
        value_fn=lambda d: d.get("phone_call") == "allow",
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
        BINARY_SENSOR_DOMAIN,
        DESCRIPTIONS,
        lambda desc: BriaBinarySensor(coordinator, desc),
        async_add_entities,
    )
    async_add_entities(
        BriaMachineConnected(coordinator, m.id, MACHINE_CONNECTED) for m in coordinator.machines
    )


class BriaBinarySensor(BriaEntity, BinarySensorEntity):
    entity_description: BriaBinarySensorDescription

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.data)


MACHINE_CONNECTED = BinarySensorEntityDescription(
    key="connected",
    translation_key="machine_connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


class BriaMachineConnected(BriaMachineEntity, BinarySensorEntity):
    """Whether Home Assistant is connected to this computer's Bria."""

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return bool(self.summary.get("connected"))
