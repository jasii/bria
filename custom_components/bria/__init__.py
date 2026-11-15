"""Bria VoIP Home Assistant integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .const import CONF_HOST, DOMAIN, PRESENCE_OPTIONS
from .coordinator import BriaCoordinator
from .entity import find_device
from .safety import check_app_control, check_dial, check_recording

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.SWITCH, Platform.NUMBER]

type BriaConfigEntry = ConfigEntry[BriaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BriaConfigEntry) -> bool:
    """Set up Bria VoIP from a config entry."""
    coordinator = BriaCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry whenever the user changes sensor group options
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_update))

    # Register services only once (idempotent guard)
    if not hass.services.has_service(DOMAIN, "set_presence"):
        _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: BriaConfigEntry) -> bool:
    """Unload a Bria config entry."""
    await entry.runtime_data.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove services when the last entry is removed
    if not [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id != entry.entry_id]:
        for svc in (
            "set_presence",
            "place_call",
            "answer_call",
            "end_call",
            "hold_call",
            "resume_call",
            "transfer_call",
            "bring_to_front",
            "sign_in",
            "sign_out",
            "set_audio_properties",
            "select_audio_devices",
            "start_attended_transfer",
            "merge_calls",
            "set_call_options",
            "check_voicemail",
            "get_contact",
            "exit",
            "send_dtmf",
            "start_call_recording",
            "stop_call_recording",
            "show_history",
            "get_call_history",
            "get_call_history_item",
            "start_im",
            "start_screen_share",
            "start_collab",
            "join_collab",
        ):
            hass.services.async_remove(DOMAIN, svc)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: key the device by entry ID instead of host, keeping its entities."""
    if entry.version > 2:
        return False  # downgrade from a future version
    if entry.version == 1:
        if old := find_device(hass, entry.entry_id, entry.data[CONF_HOST]):
            dr.async_get(hass).async_update_device(
                old.id, new_identifiers={(DOMAIN, entry.entry_id)}
            )
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.debug("Migrated Bria entry %s to version 2", entry.entry_id)
    return True


# ---------------------------------------------------------------------------
# Options update listener
# ---------------------------------------------------------------------------


async def _async_reload_on_options_update(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload integration when sensor group options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator(hass: HomeAssistant) -> BriaCoordinator | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _first_call_id(coordinator: BriaCoordinator, override: str | None) -> str | None:
    if override:
        return override
    calls = coordinator.data.get("active_calls", [])
    return calls[0]["id"] if calls else None


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------


def _register_services(hass: HomeAssistant) -> None:
    """Register all Bria action services."""

    async def handle_set_presence(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.async_set_presence(call.data["status"], call.data.get("text", ""))

    async def handle_place_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_dial(coord.entry.options, call.data["number"])
            await coord.active.async_place_call(
                call.data["number"], call.data.get("suppress_gui", False)
            )

    async def handle_answer_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if call_id:
                await coord.active.async_answer_call(call_id, call.data.get("with_video", False))

    async def handle_end_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if call_id:
                await coord.active.async_end_call(call_id)

    async def handle_hold_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if call_id:
                await coord.active.async_hold_call(call_id)

    async def handle_resume_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if call_id:
                await coord.active.async_resume_call(call_id)

    async def handle_transfer_call(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if target := call.data.get("target_number"):
                check_dial(coord.entry.options, target)
            if call_id:
                await coord.active.async_transfer_call(
                    call_id,
                    target_number=call.data.get("target_number"),
                    target_call_id=call.data.get("target_call_id"),
                )

    async def handle_bring_to_front(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_bring_to_front(call.data.get("window", "main"))

    async def handle_sign_in(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_app_control(coord.entry.options)
            await coord.active.async_sign_in(call.data["username"], call.data["password"])

    async def handle_sign_out(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_app_control(coord.entry.options)
            await coord.active.async_sign_out()

    async def handle_set_audio_properties(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_set_audio_properties(
                mute=call.data.get("mute"),
                speaker_mute=call.data.get("speaker_mute"),
                speaker=call.data.get("speaker"),
                speaker_volume=call.data.get("speaker_volume"),
                microphone_volume=call.data.get("microphone_volume"),
            )

    async def handle_select_audio_devices(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_select_audio_devices(call.data["devices"])

    async def handle_start_attended_transfer(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_dial(coord.entry.options, call.data["target_number"])
            call_id = _first_call_id(coord, call.data.get("call_id"))
            if call_id:
                await coord.active.async_start_attended_transfer(
                    call_id, call.data["target_number"]
                )

    async def handle_merge_calls(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_merge_calls(call.data["call_id"])

    async def handle_set_call_options(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_set_call_options(
                anonymous=call.data.get("anonymous"),
                letters_to_numbers=call.data.get("letters_to_numbers"),
                auto_answer=call.data.get("auto_answer"),
                call_waiting=call.data.get("call_waiting"),
            )

    async def handle_check_voicemail(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_check_voicemail(
                call.data["account_id"],
                call.data.get("suppress_gui", True),
            )

    async def handle_get_contact(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_get_contact(call.data["email"])

    async def handle_exit(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_app_control(coord.entry.options)
            await coord.active.async_exit()

    async def handle_send_dtmf(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_send_dtmf(
                call.data["digit"], call.data.get("tone", "start")
            )

    async def handle_start_call_recording(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            check_recording(coord.entry.options, call.data["filename"])
            await coord.active.async_start_call_recording(
                call.data["filename"],
                call_id=call.data.get("call_id"),
                suppress_popup=call.data.get("suppress_popup", True),
            )

    async def handle_stop_call_recording(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_stop_call_recording(call.data.get("call_id"))

    async def handle_show_history(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_show_history(
                call.data.get("history_type", "all"),
                call.data.get("text", ""),
            )

    async def handle_get_call_history(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_get_call_history(
                call.data.get("count", 10),
                call.data.get("entry_type", "all"),
            )

    async def handle_get_call_history_item(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_get_call_history_item(call.data["item_id"])

    async def handle_start_im(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_start_im(
                call.data["address"], call.data.get("im_type", "sip")
            )

    async def handle_start_screen_share(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_start_screen_share(call.data["invitees"])

    async def handle_start_collab(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_start_collab()

    async def handle_join_collab(call: ServiceCall) -> None:
        if coord := _get_coordinator(hass):
            await coord.active.async_join_collab(call.data["collab_url"])

    hass.services.async_register(
        DOMAIN,
        "set_presence",
        handle_set_presence,
        schema=vol.Schema(
            {
                vol.Required("status"): vol.In(PRESENCE_OPTIONS),
                vol.Optional("text", default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "place_call",
        handle_place_call,
        schema=vol.Schema(
            {
                vol.Required("number"): cv.string,
                vol.Optional("suppress_gui", default=False): cv.boolean,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "answer_call",
        handle_answer_call,
        schema=vol.Schema(
            {
                vol.Optional("call_id"): cv.string,
                vol.Optional("with_video", default=False): cv.boolean,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "end_call",
        handle_end_call,
        schema=vol.Schema({vol.Optional("call_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "hold_call",
        handle_hold_call,
        schema=vol.Schema({vol.Optional("call_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "resume_call",
        handle_resume_call,
        schema=vol.Schema({vol.Optional("call_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "transfer_call",
        handle_transfer_call,
        schema=vol.Schema(
            {
                vol.Optional("target_number"): cv.string,
                vol.Optional("target_call_id"): cv.string,
                vol.Optional("call_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "bring_to_front",
        handle_bring_to_front,
        schema=vol.Schema(
            {vol.Optional("window", default="main"): vol.In(["main", "collab"])}
        ),
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "sign_in",
        handle_sign_in,
        schema=vol.Schema(
            {
                vol.Required("username"): cv.string,
                vol.Required("password"): cv.string,
            }
        ),
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "sign_out",
        handle_sign_out,
        schema=vol.Schema({}),
    )

    _enabled_disabled = vol.In(["enabled", "disabled"])

    hass.services.async_register(
        DOMAIN,
        "set_audio_properties",
        handle_set_audio_properties,
        schema=vol.Schema(
            {
                vol.Optional("mute"): _enabled_disabled,
                vol.Optional("speaker_mute"): _enabled_disabled,
                vol.Optional("speaker"): _enabled_disabled,
                vol.Optional("speaker_volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Optional("microphone_volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "select_audio_devices",
        handle_select_audio_devices,
        schema=vol.Schema(
            {
                vol.Required("devices"): [
                    vol.Schema(
                        {
                            vol.Required("name"): cv.string,
                            vol.Required("id"): cv.string,
                            vol.Required("type"): vol.In(["input", "output"]),
                            vol.Required("role"): cv.string,
                        }
                    )
                ]
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "start_attended_transfer",
        handle_start_attended_transfer,
        schema=vol.Schema(
            {
                vol.Required("target_number"): cv.string,
                vol.Optional("call_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "merge_calls",
        handle_merge_calls,
        schema=vol.Schema({vol.Required("call_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "set_call_options",
        handle_set_call_options,
        schema=vol.Schema(
            {
                vol.Optional("anonymous"): _enabled_disabled,
                vol.Optional("letters_to_numbers"): _enabled_disabled,
                vol.Optional("auto_answer"): _enabled_disabled,
                vol.Optional("call_waiting"): _enabled_disabled,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "check_voicemail",
        handle_check_voicemail,
        schema=vol.Schema(
            {
                vol.Required("account_id"): cv.string,
                vol.Optional("suppress_gui", default=True): cv.boolean,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "get_contact",
        handle_get_contact,
        schema=vol.Schema({vol.Required("email"): cv.string}),
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "exit",
        handle_exit,
        schema=vol.Schema({}),
    )

    hass.services.async_register(
        DOMAIN,
        "send_dtmf",
        handle_send_dtmf,
        schema=vol.Schema(
            {
                vol.Required("digit"): vol.In(
                    ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#"]
                ),
                vol.Optional("tone", default="start"): vol.In(["start", "stop"]),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "start_call_recording",
        handle_start_call_recording,
        schema=vol.Schema(
            {
                vol.Required("filename"): cv.string,
                vol.Optional("call_id"): cv.string,
                vol.Optional("suppress_popup", default=True): cv.boolean,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "stop_call_recording",
        handle_stop_call_recording,
        schema=vol.Schema({vol.Optional("call_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "show_history",
        handle_show_history,
        schema=vol.Schema(
            {
                vol.Optional("history_type", default="all"): vol.In(["all", "missed"]),
                vol.Optional("text", default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "get_call_history",
        handle_get_call_history,
        schema=vol.Schema(
            {
                vol.Optional("count", default=10): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
                vol.Optional("entry_type", default="all"): vol.In(["all", "missed"]),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "get_call_history_item",
        handle_get_call_history_item,
        schema=vol.Schema({vol.Required("item_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        "start_im",
        handle_start_im,
        schema=vol.Schema(
            {
                vol.Required("address"): cv.string,
                vol.Optional("im_type", default="sip"): vol.In(["sip", "xmpp"]),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "start_screen_share",
        handle_start_screen_share,
        schema=vol.Schema(
            {
                vol.Required("invitees"): [
                    vol.Schema(
                        {
                            vol.Required("address"): cv.string,
                            vol.Optional("type", default="simple"): vol.In(
                                ["simple", "xmpp"]
                            ),
                        }
                    )
                ]
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "start_collab",
        handle_start_collab,
        schema=vol.Schema({}),
    )

    hass.services.async_register(
        DOMAIN,
        "join_collab",
        handle_join_collab,
        schema=vol.Schema({vol.Required("collab_url"): cv.string}),
    )
