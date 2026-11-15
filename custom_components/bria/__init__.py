"""Bria VoIP Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADDRESSES,
    CONF_HOST,
    CONF_MACHINE_ID,
    CONF_MACHINES,
    CONF_NAME,
    CONF_PORT,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .coordinator import BriaCoordinator
from .entity import find_device
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type BriaConfigEntry = ConfigEntry[BriaCoordinator]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once; they find their config entry per call."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BriaConfigEntry) -> bool:
    """Set up Bria VoIP from a config entry."""
    coordinator = BriaCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_remove_stale_devices()

    # Reload the entry whenever the user changes sensor group options
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_update))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: BriaConfigEntry) -> bool:
    """Unload a Bria config entry."""
    await entry.runtime_data.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: device keyed by entry ID. v2 -> v3: list of machines."""
    if entry.version > 3:
        return False  # downgrade from a future version
    if entry.version == 1:
        if old := find_device(hass, entry.entry_id, entry.data[CONF_HOST]):
            dr.async_get(hass).async_update_device(
                old.id, new_identifiers={(DOMAIN, entry.entry_id)}
            )
        hass.config_entries.async_update_entry(entry, version=2)
    if entry.version == 2:
        data = {
            CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT),
            CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            CONF_MACHINES: [
                {CONF_MACHINE_ID: "main", CONF_NAME: "Bria", CONF_ADDRESSES: [entry.data[CONF_HOST]]}
            ],
        }
        hass.config_entries.async_update_entry(entry, data=data, version=3)
    _LOGGER.debug("Bria entry %s is at version %s", entry.entry_id, entry.version)
    return True


# ---------------------------------------------------------------------------
# Options update listener
# ---------------------------------------------------------------------------


async def _async_reload_on_options_update(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload integration when sensor group options change."""
    await hass.config_entries.async_reload(entry.entry_id)
