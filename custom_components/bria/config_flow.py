"""Config flow for Bria VoIP."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector

from .const import (
    CONF_HOST,
    CONF_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    CONF_ALLOW_APP_CONTROL,
    CONF_ALLOW_OUTGOING_CALLS,
    CONF_ALLOW_RECORDING,
    CONF_DIAL_ALLOW_PATTERN,
    DOMAIN,
    GROUP_DEFAULTS,
    MAX_PATTERN_LENGTH,
    SAFETY_DEFAULTS,
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


def _safety_schema(current: dict[str, Any]) -> vol.Schema:
    def default(key: str) -> Any:
        return current.get(key, SAFETY_DEFAULTS[key])

    return vol.Schema(
        {
            vol.Optional(CONF_ALLOW_OUTGOING_CALLS, default=default(CONF_ALLOW_OUTGOING_CALLS)): bool,
            vol.Optional(
                CONF_DIAL_ALLOW_PATTERN,
                description={"suggested_value": default(CONF_DIAL_ALLOW_PATTERN)},
            ): TextSelector(),
            vol.Optional(CONF_ALLOW_RECORDING, default=default(CONF_ALLOW_RECORDING)): bool,
            vol.Optional(CONF_ALLOW_APP_CONTROL, default=default(CONF_ALLOW_APP_CONTROL)): bool,
        }
    )


def _validate_pattern(pattern: str) -> str | None:
    """Return an error key, or None if the dial pattern is usable."""
    if len(pattern) > MAX_PATTERN_LENGTH:
        return "pattern_too_long"
    try:
        re.compile(pattern)
    except re.error:
        return "invalid_pattern"
    return None


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
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BriaOptionsFlow()


class BriaOptionsFlow(OptionsFlow):
    """Sensor groups, then safety options."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = {**self.config_entry.options, **user_input}
            return await self.async_step_safety()

        return self.async_show_form(
            step_id="init",
            data_schema=_sensor_schema(dict(self.config_entry.options)),
        )

    async def async_step_safety(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pattern = (user_input.get(CONF_DIAL_ALLOW_PATTERN) or "").strip()
            if pattern and (error := _validate_pattern(pattern)):
                errors[CONF_DIAL_ALLOW_PATTERN] = error
            else:
                return self.async_create_entry(
                    data={**self._options, **user_input, CONF_DIAL_ALLOW_PATTERN: pattern}
                )

        current = {**self._options, **(user_input or {})}
        return self.async_show_form(
            step_id="safety", data_schema=_safety_schema(current), errors=errors
        )
