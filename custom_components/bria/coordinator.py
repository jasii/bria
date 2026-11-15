"""Bria coordinator: one or more machines merged into one view."""
from __future__ import annotations

import logging
import ssl
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ADDRESSES,
    CONF_MACHINE_ID,
    CONF_MACHINES,
    CONF_NAME,
    CONF_PORT,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .machine import BACKOFF_INITIAL, BACKOFF_MAX, BriaMachine, async_ssl_context
from .merge import merge

_LOGGER = logging.getLogger(__name__)

__all__ = ["BACKOFF_INITIAL", "BACKOFF_MAX", "BriaCoordinator", "machine_device_id"]


def machine_device_id(entry_id: str, machine_id: str) -> str:
    return f"{entry_id}_{machine_id}"


class BriaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns every machine of a config entry and publishes the merged state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None, config_entry=entry)
        self.entry = entry
        self.verify_ssl: bool = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.machines: list[BriaMachine] = [
            BriaMachine(
                hass,
                entry,
                machine_id=m[CONF_MACHINE_ID],
                name=m[CONF_NAME],
                addresses=list(m[CONF_ADDRESSES]),
                port=port,
                ssl_context=self._ssl_context,
                on_update=self._machine_updated,
            )
            for m in entry.data[CONF_MACHINES]
        ]
        self.data = merge(self.machines)

    async def _async_update_data(self) -> dict[str, Any]:
        return self.data  # push-only

    async def _ssl_context(self) -> ssl.SSLContext | None:
        """TLS context shared by all machines. Tests patch this to return None (plain ws)."""
        return await async_ssl_context(self.hass, self.verify_ssl)

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        for machine in self.machines:
            await machine.async_start()

    async def async_stop(self) -> None:
        for machine in self.machines:
            await machine.async_stop()

    @callback
    def _machine_updated(self, machine: BriaMachine) -> None:
        previous = self.data.get("system_product_version")
        machine_version = self.data.get("machines", {}).get(machine.id, {}).get("version")
        self.async_set_updated_data(merge(self.machines))
        if self.data.get("system_product_version") != previous:
            self._update_device()
        version = machine.data.get("system_product_version")
        if version and version != machine_version:
            self._update_machine_device(machine, version)

    @callback
    def _update_machine_device(self, machine: BriaMachine, version: str) -> None:
        from .entity import find_device  # noqa: PLC0415 - entity imports this module

        device_id = machine_device_id(self.entry.entry_id, machine.id)
        if device := find_device(self.hass, self.entry.entry_id, device_id):
            dr.async_get(self.hass).async_update_device(
                device.id,
                sw_version=version,
                model=machine.data.get("system_product_name") or device.model,
            )

    @callback
    def _update_device(self) -> None:
        """Keep the combined device's model and software version in step with Bria."""
        from .entity import find_device  # noqa: PLC0415 - entity imports this module

        if (device := find_device(self.hass, self.entry.entry_id, self.entry.entry_id)) is None:
            return
        dr.async_get(self.hass).async_update_device(
            device.id,
            model=self.data.get("system_product_name") or device.model,
            sw_version=self.data.get("system_product_version") or device.sw_version,
        )

    @callback
    def async_reconnect(self) -> None:
        """Retry every disconnected machine immediately."""
        for machine in self.machines:
            if not machine.connected:
                machine.reconnect()

    @callback
    def async_remove_stale_devices(self) -> None:
        """Drop child devices of computers that were removed from the entry."""
        registry = dr.async_get(self.hass)
        keep = {(DOMAIN, self.entry.entry_id)} | {
            (DOMAIN, machine_device_id(self.entry.entry_id, m.id)) for m in self.machines
        }
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            if not device.identifiers & keep:
                registry.async_update_device(device.id, remove_config_entry_id=self.entry.entry_id)

    # -- routing ---------------------------------------------------------------

    def machine(self, machine_id: str) -> BriaMachine | None:
        return next((m for m in self.machines if m.id == machine_id), None)

    def find_machine(self, name_or_id: str) -> BriaMachine | None:
        """Match a service's ``machine`` field by name (case-insensitive) or ID."""
        wanted = name_or_id.strip().casefold()
        return next(
            (m for m in self.machines if wanted in (m.name.casefold(), m.id.casefold())), None
        )

    @property
    def connected_machines(self) -> list[BriaMachine]:
        return [m for m in self.machines if m.connected]

    @property
    def active(self) -> BriaMachine:
        """The machine actions go to by default; raises if none is connected."""
        machine_id = self.data.get("active_machine")
        for machine in self.machines:
            if machine.id == machine_id and machine.connected:
                return machine
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_connected")

    async def async_set_presence(self, status: str, text: str = "") -> None:
        """Presence goes to every connected machine (decided with the user)."""
        machines = self.connected_machines
        if not machines:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_connected")
        for machine in machines:
            await machine.async_set_presence(status, text)

    async def async_set_audio_properties(self, **kwargs: Any) -> None:
        await self.active.async_set_audio_properties(**kwargs)

    async def async_set_call_options(self, **kwargs: Any) -> None:
        await self.active.async_set_call_options(**kwargs)
