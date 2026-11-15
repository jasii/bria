"""Shared entity base and platform helpers for Bria."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GROUP_DEFAULTS, GROUP_SCREEN_SHARE

if TYPE_CHECKING:
    from . import BriaConfigEntry
    from .coordinator import BriaCoordinator

MANUFACTURER = "CounterPath"


def find_device(hass: HomeAssistant, entry_id: str, identifier: str) -> dr.DeviceEntry | None:
    """Look up this entry's device by identifier (newer HA has a per-entry lookup)."""
    registry = dr.async_get(hass)
    if lookup := getattr(registry, "async_get_device_by_identifier", None):
        return lookup((DOMAIN, identifier), entry_id)
    device = registry.async_get_device(identifiers={(DOMAIN, identifier)})
    return device if device and entry_id in device.config_entries else None


def group_enabled(entry: BriaConfigEntry, group: str | None) -> bool:
    """Whether an option group is switched on (core entities have no group)."""
    if group is None:
        return True
    return bool(entry.options.get(group, GROUP_DEFAULTS.get(group, True)))


def device_info(entry: BriaConfigEntry, data: dict[str, Any]) -> DeviceInfo:
    """Device keyed by the config entry, so it survives address changes."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=data.get("system_product_name") or "Bria",
        sw_version=data.get("system_product_version"),
    )


class BriaEntity(CoordinatorEntity["BriaCoordinator"]):
    """Base for every Bria entity: one device per entry, unavailable while disconnected."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BriaCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(entry, coordinator.data)

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data

    @property
    def available(self) -> bool:
        if not self.data.get("connected"):
            return False
        group = getattr(self.entity_description, "group", None)
        if group == GROUP_SCREEN_SHARE and self.data.get("screen_share_supported") is False:
            return False
        return True


def add_entities_for_groups[D: EntityDescription](
    hass: HomeAssistant,
    entry: BriaConfigEntry,
    platform: str,
    descriptions: Sequence[D],
    factory: Callable[[D], Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for enabled groups; drop registry entries for disabled ones."""
    registry = er.async_get(hass)
    enabled: list[Entity] = []
    for desc in descriptions:
        if group_enabled(entry, getattr(desc, "group", None)):
            enabled.append(factory(desc))
            continue
        unique_id = f"{entry.entry_id}_{desc.key}"
        if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
            registry.async_remove(entity_id)
    async_add_entities(enabled)


def first_call(data: dict[str, Any]) -> dict[str, Any] | None:
    calls: Iterable[dict[str, Any]] = data.get("active_calls") or []
    return next(iter(calls), None)


def first_participant(data: dict[str, Any]) -> dict[str, Any] | None:
    call = first_call(data)
    if call and call["participants"]:
        return call["participants"][0]
    return None
