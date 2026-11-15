"""Bria presence select."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
    SelectEntity,
    SelectEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .const import DOMAIN, GROUP_PRESENCE, PRESENCE_OPTIONS, PRESENCE_STATES
from .entity import BriaEntity, add_entities_for_groups

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BriaSelectDescription(SelectEntityDescription):
    group: str | None = GROUP_PRESENCE


PRESENCE = BriaSelectDescription(key="presence_status", translation_key="presence_status")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_entities_for_groups(
        hass,
        entry,
        SELECT_DOMAIN,
        (PRESENCE,),
        lambda desc: BriaPresenceSelect(coordinator, desc),
        async_add_entities,
    )


class BriaPresenceSelect(BriaEntity, SelectEntity):
    """Shows every status Bria reports; only the ones Bria lists as supported can be set.

    Statuses such as ``onThePhone`` or ``idle`` are set by Bria itself
    (p. 10: set one of the values from ``supportedPresence``).
    """

    entity_description: BriaSelectDescription
    _attr_options = list(PRESENCE_STATES)

    def _settable(self) -> list[str]:
        return list(self.data.get("supported_presences") or PRESENCE_OPTIONS)

    @property
    def current_option(self) -> str | None:
        status = self.data.get("presence_status")
        return status if status in PRESENCE_STATES else None

    async def async_select_option(self, option: str) -> None:
        if option not in self._settable():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="presence_not_settable",
                translation_placeholders={"status": option},
            )
        await self.coordinator.async_set_presence(option)
