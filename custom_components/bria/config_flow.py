"""Config, options and reconfigure flows for Bria VoIP."""
from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ADDRESSES,
    CONF_ALLOW_APP_CONTROL,
    CONF_ALLOW_OUTGOING_CALLS,
    CONF_ALLOW_RECORDING,
    CONF_DIAL_ALLOW_PATTERN,
    CONF_MACHINE_ID,
    CONF_MACHINES,
    CONF_NAME,
    CONF_PORT,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    GROUP_DEFAULTS,
    MAX_ADDRESSES,
    MAX_MACHINES,
    MAX_PATTERN_LENGTH,
    SAFETY_DEFAULTS,
    SENSOR_GROUPS,
)
from .machine import async_ssl_context, async_test_address

CONF_SAVE_ANYWAY = "save_anyway"
CONF_MACHINE = "machine"

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^(?=.{{1,253}}$){_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?$")
_PORT_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


class AddressError(Exception):
    """Address list is unusable; ``args[0]`` is the error key."""


def normalise_address(raw: str) -> str:
    """Strip scheme and path; validate as IP or hostname with an optional ``:port``.

    Returns ``host``, ``host:port`` or ``[v6]:port``.
    """
    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", raw.strip())
    value = value.split("/", 1)[0]
    port: str | None = None
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        if rest:
            if not rest.startswith(":"):
                raise AddressError("invalid_address")
            port = rest[1:]
    elif value.count(":") == 1:
        host, port = value.split(":")
    else:
        host = value
    if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise AddressError("invalid_address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.match(host):
            raise AddressError("invalid_address") from None
        host = host.lower().rstrip(".")
    else:
        host = f"[{ip}]" if ip.version == 6 and port else str(ip)
    return f"{host}:{int(port)}" if port else host


def parse_addresses(text: str) -> list[str]:
    result: list[str] = []
    for raw in re.split(r"[\s,;]+", text.strip()):
        if raw and (address := normalise_address(raw)) not in result:
            result.append(address)
    if not result:
        raise AddressError("no_addresses")
    if len(result) > MAX_ADDRESSES:
        raise AddressError("too_many_addresses")
    return result


def addresses_in_use(hass: HomeAssistant, exclude_entry: str | None = None) -> set[str]:
    used: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry:
            continue
        for machine in entry.data.get(CONF_MACHINES, []):
            used.update(machine.get(CONF_ADDRESSES, []))
    return used


def _machine_schema(name: str = "", addresses: str = "", *, offer_save_anyway: bool = False) -> vol.Schema:
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=name): TextSelector(),
        vol.Required(CONF_ADDRESSES, default=addresses): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
    }
    if offer_save_anyway:
        schema[vol.Optional(CONF_SAVE_ANYWAY, default=False)] = bool
    return vol.Schema(schema)


def _sensor_schema(defaults: dict | None = None) -> vol.Schema:
    """Sensor group switches, defaulting to current values."""
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


_RESULT_TEXT = {
    None: "connected",
    "cannot_connect": "no answer on the port",
    "ssl_error": "certificate could not be verified",
    "not_authorized": "connected, but Bria did not answer (click Allow Access in Bria)",
}


class MachineFormMixin:
    """Shared "name + addresses + connection test" handling for both flows."""

    hass: HomeAssistant
    _failed_test: bool = False

    def _port_and_verify(self) -> tuple[int, bool]:
        raise NotImplementedError

    def _taken_names(self) -> set[str]:
        raise NotImplementedError

    def _taken_addresses(self) -> set[str]:
        raise NotImplementedError

    async def _check_machine(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, str], str]:
        """Validate and test. Returns (machine or None, errors, results text)."""
        errors: dict[str, str] = {}
        name = user_input[CONF_NAME].strip()
        if not name:
            errors[CONF_NAME] = "name_required"
        elif name.casefold() in {n.casefold() for n in self._taken_names()}:
            errors[CONF_NAME] = "name_in_use"
        try:
            addresses = parse_addresses(user_input[CONF_ADDRESSES])
        except AddressError as err:
            errors[CONF_ADDRESSES] = err.args[0]
            return None, errors, ""
        if self._taken_addresses() & set(addresses):
            errors[CONF_ADDRESSES] = "address_in_use"
        if errors:
            return None, errors, ""

        port, verify = self._port_and_verify()
        ctx = await async_ssl_context(self.hass, verify)
        outcomes = await asyncio.gather(
            *(async_test_address(self.hass, a, port, ctx) for a in addresses)
        )
        results = "\n".join(f"- `{a}`: {_RESULT_TEXT[o]}" for a, o in zip(addresses, outcomes, strict=True))
        machine = {CONF_MACHINE_ID: uuid.uuid4().hex[:8], CONF_NAME: name, CONF_ADDRESSES: addresses}
        if any(o is None for o in outcomes) or user_input.get(CONF_SAVE_ANYWAY):
            return machine, {}, results
        self._failed_test = True
        first_error = next(o for o in outcomes if o is not None)
        return None, {"base": first_error}, results


class BriaConfigFlow(MachineFormMixin, ConfigFlow, domain=DOMAIN):
    """Add Bria: first computer, then sensor groups."""

    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._port = DEFAULT_PORT

    def _port_and_verify(self) -> tuple[int, bool]:
        return self._port, DEFAULT_VERIFY_SSL

    def _taken_names(self) -> set[str]:
        return set()

    def _taken_addresses(self) -> set[str]:
        return addresses_in_use(self.hass)

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        results = ""
        if user_input is not None:
            self._port = user_input.get(CONF_PORT, DEFAULT_PORT)
            machine, errors, results = await self._check_machine(user_input)
            if machine is not None:
                await self.async_set_unique_id(uuid.uuid4().hex)
                self._data = {
                    CONF_PORT: self._port,
                    CONF_VERIFY_SSL: DEFAULT_VERIFY_SSL,
                    CONF_MACHINES: [machine],
                }
                return await self.async_step_sensors()

        schema = _machine_schema(
            (user_input or {}).get(CONF_NAME, "Desktop"),
            (user_input or {}).get(CONF_ADDRESSES, ""),
            offer_save_anyway=self._failed_test,
        ).extend({vol.Required(CONF_PORT, default=self._port): _PORT_SCHEMA})
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"results": results},
        )

    async def async_step_sensors(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Bria", data=self._data, options=user_input)
        return self.async_show_form(step_id="sensors", data_schema=_sensor_schema())

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Port and certificate verification; computers are managed under Configure."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)): _PORT_SCHEMA,
                    vol.Required(
                        CONF_VERIFY_SSL, default=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
                    ): bool,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BriaOptionsFlow()


class BriaOptionsFlow(MachineFormMixin, OptionsFlow):
    """Menu: computers, sensor groups, safety."""

    def __init__(self) -> None:
        self._edit_id: str | None = None

    # -- helpers ----------------------------------------------------------------

    @property
    def _machines(self) -> list[dict[str, Any]]:
        return list(self.config_entry.data.get(CONF_MACHINES, []))

    def _port_and_verify(self) -> tuple[int, bool]:
        data = self.config_entry.data
        return data.get(CONF_PORT, DEFAULT_PORT), data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

    def _others(self) -> list[dict[str, Any]]:
        return [m for m in self._machines if m[CONF_MACHINE_ID] != self._edit_id]

    def _taken_names(self) -> set[str]:
        return {m[CONF_NAME] for m in self._others()}

    def _taken_addresses(self) -> set[str]:
        used = addresses_in_use(self.hass, exclude_entry=self.config_entry.entry_id)
        for m in self._others():
            used.update(m[CONF_ADDRESSES])
        return used

    def _save_machines(self, machines: list[dict[str, Any]]) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_MACHINES: machines}
        )
        return self.async_create_entry(data=dict(self.config_entry.options))

    def _machine_selector(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_MACHINE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": m[CONF_MACHINE_ID], "label": m[CONF_NAME]} for m in self._machines
                        ]
                    )
                )
            }
        )

    # -- menu ---------------------------------------------------------------------

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_machine", "edit_machine", "remove_machine", "groups", "safety"],
        )

    async def async_step_add_machine(self, user_input: dict | None = None) -> ConfigFlowResult:
        if len(self._machines) >= MAX_MACHINES:
            return self.async_abort(reason="too_many_machines")
        return await self._machine_form("add_machine", user_input, None)

    async def async_step_edit_machine(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_id = user_input[CONF_MACHINE]
            return await self.async_step_edit_machine_details()
        return self.async_show_form(step_id="edit_machine", data_schema=self._machine_selector())

    async def async_step_edit_machine_details(self, user_input: dict | None = None) -> ConfigFlowResult:
        current = next(m for m in self._machines if m[CONF_MACHINE_ID] == self._edit_id)
        return await self._machine_form("edit_machine_details", user_input, current)

    async def _machine_form(
        self, step_id: str, user_input: dict | None, current: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        results = ""
        if user_input is not None:
            machine, errors, results = await self._check_machine(user_input)
            if machine is not None:
                if current is None:
                    return self._save_machines([*self._machines, machine])
                machine[CONF_MACHINE_ID] = current[CONF_MACHINE_ID]  # keep identity
                return self._save_machines(
                    [machine if m[CONF_MACHINE_ID] == current[CONF_MACHINE_ID] else m for m in self._machines]
                )
        source = user_input or (
            {CONF_NAME: current[CONF_NAME], CONF_ADDRESSES: "\n".join(current[CONF_ADDRESSES])}
            if current
            else {}
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=_machine_schema(
                source.get(CONF_NAME, ""),
                source.get(CONF_ADDRESSES, ""),
                offer_save_anyway=self._failed_test,
            ),
            errors=errors,
            description_placeholders={"results": results},
        )

    async def async_step_remove_machine(self, user_input: dict | None = None) -> ConfigFlowResult:
        if len(self._machines) <= 1:
            return self.async_abort(reason="last_machine")
        if user_input is not None:
            return self._save_machines(
                [m for m in self._machines if m[CONF_MACHINE_ID] != user_input[CONF_MACHINE]]
            )
        return self.async_show_form(step_id="remove_machine", data_schema=self._machine_selector())

    async def async_step_groups(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(
            step_id="groups", data_schema=_sensor_schema(dict(self.config_entry.options))
        )

    async def async_step_safety(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pattern = (user_input.get(CONF_DIAL_ALLOW_PATTERN) or "").strip()
            if pattern and (error := _validate_pattern(pattern)):
                errors[CONF_DIAL_ALLOW_PATTERN] = error
            else:
                return self.async_create_entry(
                    data={**self.config_entry.options, **user_input, CONF_DIAL_ALLOW_PATTERN: pattern}
                )
        current = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="safety", data_schema=_safety_schema(current), errors=errors)
