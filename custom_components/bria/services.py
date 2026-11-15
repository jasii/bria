"""Bria services, registered once and routed to the right computer.

Routing (PLAN 3d):
- Calls: the computer that owns ``call_id``; without one, the focus call
  (ringing first). ``answer_call`` prefers a ringing call.
- ``set_presence``: every connected computer.
- ``sign_in``, ``sign_out``, ``exit``, ``check_voicemail``: ``machine`` is
  required when the entry has more than one computer.
- Everything else: ``machine`` if given, else the active computer.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN, PRESENCE_OPTIONS
from .coordinator import BriaCoordinator
from .machine import BriaMachine, _jsonable
from .safety import check_app_control, check_dial, check_recording

ATTR_MACHINE = "machine"
ATTR_CALL_ID = "call_id"

_ENABLED_DISABLED = vol.In(["enabled", "disabled"])
_VOLUME = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
_DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#"]

_BASE = {vol.Optional(ATTR_MACHINE): cv.string}
_CALL = {**_BASE, vol.Optional(ATTR_CALL_ID): cv.string}


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


# -- resolution ------------------------------------------------------------------------


def _coordinators(hass: HomeAssistant) -> list[BriaCoordinator]:
    return [
        e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> BriaCoordinator:
    coordinators = _coordinators(hass)
    if not coordinators:
        raise _error("not_configured")
    if name := call.data.get(ATTR_MACHINE):
        for coord in coordinators:
            if coord.find_machine(name):
                return coord
        raise _error("unknown_machine", machine=name)
    if len(coordinators) > 1:
        raise _error("several_entries")
    return coordinators[0]


def _named(coord: BriaCoordinator, call: ServiceCall) -> BriaMachine | None:
    if name := call.data.get(ATTR_MACHINE):
        machine = coord.find_machine(name)
        if machine is None:
            raise _error("unknown_machine", machine=name)
        if not machine.connected:
            raise _error("machine_not_connected", machine=machine.name)
        return machine
    return None


def target_machine(coord: BriaCoordinator, call: ServiceCall, *, required: bool = False) -> BriaMachine:
    if machine := _named(coord, call):
        return machine
    if required and len(coord.machines) > 1:
        raise _error("machine_required")
    return coord.active


def target_call(
    coord: BriaCoordinator, call: ServiceCall, *, prefer_ringing: bool = False
) -> tuple[BriaMachine, str]:
    """(machine, call id) for a call action."""
    named = _named(coord, call)
    call_id = call.data.get(ATTR_CALL_ID)
    calls = [
        c
        for c in coord.data.get("active_calls", [])
        if (named is None or c["machine"] == named.id) and (call_id is None or c["id"] == call_id)
    ]
    if prefer_ringing:
        ringing = [
            c for c in calls if any(p.get("state") == "ringing" for p in c.get("participants", []))
        ]
        calls = ringing or calls
    if calls:
        machine = coord.machine(calls[0]["machine"])
        if machine is not None and machine.connected:
            return machine, calls[0]["id"]
    if call_id:
        # Unknown to us (for example a call that just started): trust the caller.
        return named or coord.active, call_id
    raise _error("no_active_call")


# -- handlers ---------------------------------------------------------------------------

type Handler = Callable[[HomeAssistant, ServiceCall], Awaitable[ServiceResponse]]


async def _set_presence(hass: HomeAssistant, call: ServiceCall) -> None:
    coord = _coordinator(hass, call)
    if machine := _named(coord, call):
        await machine.async_set_presence(call.data["status"], call.data["text"])
    else:
        await coord.async_set_presence(call.data["status"], call.data["text"])


async def _place_call(hass: HomeAssistant, call: ServiceCall) -> None:
    coord = _coordinator(hass, call)
    check_dial(coord.entry.options, call.data["number"])
    await target_machine(coord, call).async_place_call(
        call.data["number"],
        call.data["suppress_gui"],
        call_type=call.data["call_type"],
        display_name=call.data.get("display_name"),
        account_id=call.data.get("account_id"),
    )


async def _answer_call(hass: HomeAssistant, call: ServiceCall) -> None:
    machine, call_id = target_call(_coordinator(hass, call), call, prefer_ringing=True)
    await machine.async_answer_call(call_id, call.data["with_video"])


def _simple_call_action(method: str) -> Handler:
    async def handler(hass: HomeAssistant, call: ServiceCall) -> None:
        machine, call_id = target_call(_coordinator(hass, call), call)
        await getattr(machine, method)(call_id)

    return handler


async def _transfer_call(hass: HomeAssistant, call: ServiceCall) -> None:
    coord = _coordinator(hass, call)
    if target := call.data.get("target_number"):
        check_dial(coord.entry.options, target)
    machine, call_id = target_call(coord, call)
    await machine.async_transfer_call(
        call_id, target_number=target, target_call_id=call.data.get("target_call_id")
    )


async def _start_attended_transfer(hass: HomeAssistant, call: ServiceCall) -> None:
    coord = _coordinator(hass, call)
    check_dial(coord.entry.options, call.data["target_number"])
    machine, call_id = target_call(coord, call)
    await machine.async_start_attended_transfer(call_id, call.data["target_number"])


async def _start_call_recording(hass: HomeAssistant, call: ServiceCall) -> None:
    coord = _coordinator(hass, call)
    check_recording(coord.entry.options, call.data["filename"])
    machine, call_id = target_call(coord, call)
    await machine.async_start_call_recording(
        call.data["filename"], call_id=call_id, suppress_popup=call.data["suppress_popup"]
    )


async def _stop_call_recording(hass: HomeAssistant, call: ServiceCall) -> None:
    machine, call_id = target_call(_coordinator(hass, call), call)
    await machine.async_stop_call_recording(call_id)


async def _send_dtmf(hass: HomeAssistant, call: ServiceCall) -> None:
    machine, _ = target_call(_coordinator(hass, call), call)
    await machine.async_send_dtmf(call.data["digit"], call.data["tone"])


def _app_control(method: str, *args: str) -> Handler:
    async def handler(hass: HomeAssistant, call: ServiceCall) -> None:
        coord = _coordinator(hass, call)
        check_app_control(coord.entry.options)
        machine = target_machine(coord, call, required=True)
        await getattr(machine, method)(*(call.data[a] for a in args))

    return handler


def _machine_action(method: str, *args: str, kwargs: tuple[str, ...] = ()) -> Handler:
    """Plain action on the named or active machine; arguments taken from service data."""

    async def handler(hass: HomeAssistant, call: ServiceCall) -> None:
        machine = target_machine(_coordinator(hass, call), call)
        await getattr(machine, method)(
            *(call.data[a] for a in args), **{k: call.data.get(k) for k in kwargs}
        )

    return handler


async def _check_voicemail(hass: HomeAssistant, call: ServiceCall) -> None:
    machine = target_machine(_coordinator(hass, call), call, required=True)
    await machine.async_check_voicemail(call.data["account_id"], call.data["suppress_gui"])


async def _get_contact(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    machine = target_machine(_coordinator(hass, call), call)
    return {**(await machine.async_get_contact(call.data["email"])), ATTR_MACHINE: machine.name}


async def _get_call_history(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    machine = target_machine(_coordinator(hass, call), call)
    entries = await machine.async_get_call_history(call.data["count"], call.data["entry_type"])
    return {"entries": _jsonable(entries), ATTR_MACHINE: machine.name}


async def _get_call_history_item(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    machine = target_machine(_coordinator(hass, call), call)
    entries = await machine.async_get_call_history_item(call.data["item_id"])
    return {"entries": _jsonable(entries), ATTR_MACHINE: machine.name}


# -- registration -------------------------------------------------------------------------

_DEVICE = vol.Schema({vol.Required("name"): cv.string, vol.Required("id"): cv.string,
                      vol.Required("type"): vol.In(["input", "output"]), vol.Required("role"): cv.string})
_INVITEE = vol.Schema({vol.Required("address"): cv.string,
                       vol.Optional("type", default="simple"): vol.In(["simple", "xmpp"])})

# name -> (handler, schema, admin, response)
SERVICES: dict[str, tuple[Handler, dict[Any, Any], bool, SupportsResponse]] = {
    "set_presence": (_set_presence, {**_BASE, vol.Required("status"): vol.In(PRESENCE_OPTIONS),
                                     vol.Optional("text", default=""): cv.string}, False, SupportsResponse.NONE),
    "place_call": (_place_call, {**_BASE, vol.Required("number"): cv.string,
                                 vol.Optional("suppress_gui", default=False): cv.boolean,
                                 vol.Optional("call_type", default="audio"): vol.In(["audio", "video", "conference"]),
                                 vol.Optional("display_name"): cv.string,
                                 vol.Optional("account_id"): cv.string}, False, SupportsResponse.NONE),
    "answer_call": (_answer_call, {**_CALL, vol.Optional("with_video", default=False): cv.boolean},
                    False, SupportsResponse.NONE),
    "end_call": (_simple_call_action("async_end_call"), _CALL, False, SupportsResponse.NONE),
    "hold_call": (_simple_call_action("async_hold_call"), _CALL, False, SupportsResponse.NONE),
    "resume_call": (_simple_call_action("async_resume_call"), _CALL, False, SupportsResponse.NONE),
    "merge_calls": (_simple_call_action("async_merge_calls"), _CALL, False, SupportsResponse.NONE),
    "transfer_call": (_transfer_call, {**_CALL, vol.Optional("target_number"): cv.string,
                                       vol.Optional("target_call_id"): cv.string}, False, SupportsResponse.NONE),
    "start_attended_transfer": (_start_attended_transfer, {**_CALL, vol.Required("target_number"): cv.string},
                                False, SupportsResponse.NONE),
    "send_dtmf": (_send_dtmf, {**_CALL, vol.Required("digit"): vol.In(_DIGITS),
                               vol.Optional("tone", default="start"): vol.In(["start", "stop"])},
                  False, SupportsResponse.NONE),
    "start_call_recording": (_start_call_recording, {**_CALL, vol.Required("filename"): cv.string,
                                                     vol.Optional("suppress_popup", default=True): cv.boolean},
                             False, SupportsResponse.NONE),
    "stop_call_recording": (_stop_call_recording, _CALL, False, SupportsResponse.NONE),
    "bring_to_front": (_machine_action("async_bring_to_front", "window"),
                       {**_BASE, vol.Optional("window", default="main"): vol.In(["main", "collab"])},
                       False, SupportsResponse.NONE),
    "set_audio_properties": (
        _machine_action("async_set_audio_properties",
                        kwargs=("mute", "speaker_mute", "speaker", "speaker_volume", "microphone_volume")),
        {**_BASE, vol.Optional("mute"): _ENABLED_DISABLED, vol.Optional("speaker_mute"): _ENABLED_DISABLED,
         vol.Optional("speaker"): _ENABLED_DISABLED, vol.Optional("speaker_volume"): _VOLUME,
         vol.Optional("microphone_volume"): _VOLUME},
        False, SupportsResponse.NONE),
    "select_audio_devices": (_machine_action("async_select_audio_devices", "devices"),
                             {**_BASE, vol.Required("devices"): [_DEVICE]}, False, SupportsResponse.NONE),
    "set_call_options": (
        _machine_action("async_set_call_options",
                        kwargs=("anonymous", "letters_to_numbers", "auto_answer", "call_waiting")),
        {**_BASE, vol.Optional("anonymous"): _ENABLED_DISABLED,
         vol.Optional("letters_to_numbers"): _ENABLED_DISABLED,
         vol.Optional("auto_answer"): _ENABLED_DISABLED, vol.Optional("call_waiting"): _ENABLED_DISABLED},
        False, SupportsResponse.NONE),
    "check_voicemail": (_check_voicemail, {**_BASE, vol.Required("account_id"): cv.string,
                                           vol.Optional("suppress_gui", default=True): cv.boolean},
                        False, SupportsResponse.NONE),
    "show_history": (_machine_action("async_show_history", "history_type", "text"),
                     {**_BASE, vol.Optional("history_type", default="all"): vol.In(["all", "missed"]),
                      vol.Optional("text", default=""): cv.string}, False, SupportsResponse.NONE),
    "start_im": (_machine_action("async_start_im", "address", "im_type"),
                 {**_BASE, vol.Required("address"): cv.string,
                  vol.Optional("im_type", default="sip"): vol.In(["sip", "xmpp"])}, False, SupportsResponse.NONE),
    "start_screen_share": (_machine_action("async_start_screen_share", "invitees"),
                           {**_BASE, vol.Required("invitees"): [_INVITEE]}, False, SupportsResponse.NONE),
    "start_collab": (_machine_action("async_start_collab"), _BASE, False, SupportsResponse.NONE),
    "join_collab": (_machine_action("async_join_collab", "collab_url"),
                    {**_BASE, vol.Required("collab_url"): cv.string}, False, SupportsResponse.NONE),
    "get_contact": (_get_contact, {**_BASE, vol.Required("email"): cv.string}, False, SupportsResponse.OPTIONAL),
    "get_call_history": (_get_call_history, {**_BASE,
                                             vol.Optional("count", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                                             vol.Optional("entry_type", default="all"): vol.In(["all", "missed"])},
                         False, SupportsResponse.OPTIONAL),
    "get_call_history_item": (_get_call_history_item, {**_BASE, vol.Required("item_id"): cv.string},
                              False, SupportsResponse.OPTIONAL),
    "sign_in": (_app_control("async_sign_in", "username", "password"),
                {**_BASE, vol.Required("username"): cv.string, vol.Required("password"): cv.string},
                True, SupportsResponse.NONE),
    "sign_out": (_app_control("async_sign_out"), _BASE, True, SupportsResponse.NONE),
    "exit": (_app_control("async_exit"), _BASE, True, SupportsResponse.NONE),
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, (handler, schema, admin, response) in SERVICES.items():

        async def service(call: ServiceCall, _handler: Handler = handler) -> ServiceResponse:
            result = await _handler(hass, call)
            return result if call.return_response else None

        if admin:
            async_register_admin_service(hass, DOMAIN, name, service, vol.Schema(schema))
        else:
            hass.services.async_register(
                DOMAIN, name, service, schema=vol.Schema(schema), supports_response=response
            )
