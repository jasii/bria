"""Config flow for Bria VoIP."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithConfigEntry
from homeassistant.core import callback

from .const import (
    CONF_HOST,
    CONF_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
    GROUP_DEFAULTS,
    SENSOR_GROUPS,
)

_CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
    }
)


def _sensor_schema(defaults: dict | None = None) -> vol.Schema:
    """Build the sensor group selection schema with optional current-value defaults."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(group, default=d.get(group, GROUP_DEFAULTS[group])): bool
            for group in SENSOR_GROUPS
        }
    )


class BriaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow for Bria VoIP."""

    VERSION = 2

    def __init__(self) -> None:
        self._connection_data: dict = {}

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            unique_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            self._connection_data = user_input
            return await self.async_step_sensors()

        return self.async_show_form(step_id="user", data_schema=_CONNECTION_SCHEMA)

    async def async_step_sensors(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="Bria VoIP",
                data=self._connection_data,
                options=user_input,
            )

        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensor_schema(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithConfigEntry:
        return BriaOptionsFlow(config_entry)


class BriaOptionsFlow(OptionsFlowWithConfigEntry):
    """Allow the user to re-configure sensor groups after initial setup."""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_sensor_schema(dict(self.config_entry.options)),
        )
