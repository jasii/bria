"""One Bria computer: its connection loop, its state and its actions."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
import copy
from datetime import UTC, datetime
import logging
from pathlib import Path
import ssl
import time
from typing import Any
import xml.etree.ElementTree as ET

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import (
    SSL_ALPN_HTTP11,
    SSLCipherList,
    create_client_context,
    get_default_no_verify_context,
)

from .api import (
    BriaBadRequest,
    BriaClient,
    BriaConnectionError,
    BriaError,
    BriaNotReady,
    BriaTimeout,
    BriaTLSError,
    BriaResponse,
    xml_el,
)
from .const import (
    DOMAIN,
    STATUS_ACCOUNT,
    STATUS_AUDIO_DEVICES,
    STATUS_AUDIO_PROPERTIES,
    STATUS_AUTHENTICATION,
    STATUS_CALL,
    STATUS_CALL_OPTIONS,
    STATUS_CALLHISTORY,
    STATUS_MISSED_CALL,
    STATUS_PHONE,
    STATUS_PRESENCE,
    STATUS_SCREENSHARE,
    STATUS_SUPPORTED_PRESENCE,
    STATUS_SYSTEM_INFO,
    STATUS_SYSTEM_SETTINGS,
    STATUS_VOICEMAIL,
)

_LOGGER = logging.getLogger(__name__)

# Bria serves its certificate without the issuing intermediate, so a plain
# certifi store cannot build the chain. Ship the public intermediate.
INTERMEDIATE_CA = Path(__file__).parent / "certs" / "rapidssl_tls_rsa_ca_g1.pem"


def build_verify_context() -> ssl.SSLContext:
    """Default client context plus Bria's intermediate CA. Blocking: run in an executor."""
    ctx = create_client_context(SSLCipherList.PYTHON_DEFAULT, SSL_ALPN_HTTP11)
    ctx.load_verify_locations(cafile=str(INTERMEDIATE_CA))
    return ctx


_VERIFY_CONTEXT: ssl.SSLContext | None = None


def split_address(address: str, default_port: int) -> tuple[str, int]:
    """``host``, ``host:port`` or ``[v6]:port`` -> (host, port). Bare IPv6 has no port."""
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if address.count(":") == 1:
        host, port = address.split(":")
        return host, int(port)
    return address, default_port


async def async_ssl_context(hass: HomeAssistant, verify: bool) -> ssl.SSLContext:
    """Verifying context (built once, off the event loop) or HA's no-verify context."""
    global _VERIFY_CONTEXT  # noqa: PLW0603 - immutable once built, shared by all entries
    if not verify:
        return get_default_no_verify_context()
    if _VERIFY_CONTEXT is None:
        _VERIFY_CONTEXT = await hass.async_add_executor_job(build_verify_context)
    return _VERIFY_CONTEXT


async def async_test_address(
    hass: HomeAssistant, address: str, port: int, ssl_context: ssl.SSLContext | None
) -> str | None:
    """Connect once and ask for systemInformation. Returns an error key or None.

    ``not_authorized`` means the socket opened but Bria did not answer, which is
    what an unanswered Allow Access prompt looks like.
    """
    client = BriaClient(async_get_clientsession(hass))
    host, port = split_address(address, port)
    try:
        await client.connect(host, port, ssl_context=ssl_context, timeout=TEST_TIMEOUT)
    except BriaTLSError:
        return "ssl_error"
    except BriaConnectionError:
        return "cannot_connect"
    runner = asyncio.create_task(client.run())
    try:
        await client.status(STATUS_SYSTEM_INFO, timeout=TEST_TIMEOUT)
    except (BriaTimeout, BriaNotReady):
        return "not_authorized"
    except BriaError:
        return "cannot_connect"
    finally:
        await client.close()
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
    return None

BACKOFF_INITIAL = 5.0
BACKOFF_MAX = 60.0
STABLE_CONNECTION = 30.0  # a connection that lasted this long resets the backoff
HISTORY_COUNT = 50
TEST_TIMEOUT = 5.0

_DEFAULT_DATA: dict[str, Any] = {
    "connected": False,
    "presence_status": None,
    "presence_text": None,
    "authenticated": None,
    "auth_reason": None,
    "auth_server_reason": None,
    "active_calls": [],
    "screen_share_supported": None,
    "screen_share_status": None,
    "screen_share_url": None,
    "phone_state": None,
    "phone_call": None,
    "phone_max_lines": None,
    "phone_account_status": None,
    "phone_account_failure_code": None,
    "system_company_name": None,
    "system_product_name": None,
    "system_product_version": None,
    "system_product_build": None,
    "system_default_call_type": None,
    "system_call_right_away": None,
    "audio_devices": [],
    "audio_mute": None,
    "audio_speaker_mute": None,
    "audio_speaker": None,
    "audio_speaker_volume": None,
    "audio_microphone_volume": None,
    "call_option_anonymous": None,
    "call_option_letters_to_numbers": None,
    "call_option_auto_answer": None,
    "call_option_call_waiting": None,
    "missed_call_count": None,
    "voicemail_accounts": [],
    "sip_accounts": [],
    "xmpp_accounts": [],
    "supported_presences": [],
    "call_history": [],
}


def _default_data() -> dict[str, Any]:
    return copy.deepcopy(_DEFAULT_DATA)


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-testable)
# ---------------------------------------------------------------------------


def _text(el: ET.Element, tag: str) -> str | None:
    val = el.findtext(tag)
    return val.strip() if val is not None else None


def _int(value: str | None) -> int | None:
    try:
        return int(value.strip()) if value is not None else None
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in ("true", "enabled")


def _timestamp(value: str | None) -> datetime | None:
    """Unix seconds -> aware datetime. 0 means 'not connected yet' (p. 33)."""
    secs = _int(value)
    if not secs:
        return None
    return datetime.fromtimestamp(secs, tz=UTC)


def parse_calls(root: ET.Element) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in root.findall("call"):
        participants = [
            {
                "number": _text(p, "number") or "",
                "display_name": _text(p, "displayName") or "",
                "state": _text(p, "state") or "",
                "start_time": _timestamp(p.findtext("timeInitiated")),
            }
            for p in call.findall("participants/participant")
        ]
        hold_status = _text(call, "holdStatus") or ""
        calls.append(
            {
                "id": _text(call, "id") or "",
                "hold_status": hold_status,
                # offHold / localHold / remoteHold (p. 33); the example also shows onHold.
                "hold": hold_status not in ("", "offHold"),
                "recording": _text(call, "recordingStatus") or "",
                "recording_file": _text(call, "recordingFile") or "",
                "participants": participants,
            }
        )
    return calls


def parse_history(root: ET.Element) -> list[dict[str, Any]]:
    return [
        {
            "type": _text(e, "type") or "",
            "number": _text(e, "number") or "",
            "display_name": _text(e, "displayName") or "",
            "account_id": _text(e, "accountId") or "",
            "duration": _int(e.findtext("duration")),
            "time_initiated": _timestamp(e.findtext("timeInitiated")),
            "id": _text(e, "id") or "",
        }
        for e in root.findall("callHistory")
    ]


def parse_accounts(root: ET.Element) -> list[dict[str, Any]]:
    return [
        {
            "account_name": _text(a, "accountName") or "",
            "account_id": _text(a, "accountId") or "",
            "type": _text(a, "type") or "",
            "enabled": bool(_bool(a.findtext("enabled"))),
            "registered": bool(_bool(a.findtext("registered"))),
        }
        for a in root.findall("account")
    ]


def parse_audio_devices(root: ET.Element) -> list[dict[str, Any]]:
    devices = []
    for d in root.iter("device"):
        sel = d.find("selected")
        roles = (sel.get("role", "") if sel is not None else "").split(",")
        devices.append(
            {
                "name": _text(d, "name") or "",
                "id": _text(d, "id") or "",
                "type": _text(d, "type") or "",
                "selected": bool(_bool(sel.text if sel is not None else None)),
                "roles": [r.strip() for r in roles if r.strip()],
            }
        )
    return devices


def parse_contact(root: ET.Element | None) -> dict[str, Any] | None:
    contact = root.find("contact") if root is not None else None
    if contact is None:
        return None
    data: dict[str, Any] = {
        tag: _text(contact, xml_tag)
        for tag, xml_tag in (
            ("id", "id"),
            ("presence_status", "presenceStatus"),
            ("presence_text", "presenceText"),
            ("presence_address", "presenceAddress"),
            ("presence_type", "presenceType"),
            ("collab_url", "collabUrl"),
        )
    }
    for tag in ("email", "work", "mobile", "home", "fax", "jid", "other", "softphone", "website"):
        values = [el.text.strip() for el in contact.findall(tag) if el.text and el.text.strip()]
        if values:
            data[tag] = values if len(values) > 1 else values[0]
    return data


def version_tuple(version: str | None) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in (version or "").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------


class BriaMachine:
    """Keeps one connection to one Bria computer alive and mirrors its state into ``data``.

    ``addresses`` reach the same computer (for example LAN and Tailscale); they are
    tried in order on every attempt. ``on_update`` is called after every change.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        machine_id: str,
        name: str,
        addresses: list[str],
        port: int,
        ssl_context: Callable[[], Awaitable[ssl.SSLContext | None]],
        on_update: Callable[[BriaMachine], None],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.id = machine_id
        self.name = name
        self.addresses = addresses
        self.port = port
        self.address: str | None = None  # the address currently connected
        self.last_activity = 0.0  # monotonic time of the last call/presence change
        self._ssl_context = ssl_context
        self._on_update = on_update
        self.client = BriaClient(async_get_clientsession(hass), on_event=self._on_event)
        self.data = _default_data()
        self._loop_task: asyncio.Task | None = None
        self._failures = 0

    @property
    def connected(self) -> bool:
        return bool(self.data.get("connected"))

    @property
    def _issue_id(self) -> str:
        return f"tls_{self.entry.entry_id}_{self.id}"

    def _notify(self) -> None:
        self._on_update(self)

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        self._loop_task = self.entry.async_create_background_task(
            self.hass, self._connection_loop(), f"{DOMAIN} connection {self.name}"
        )

    async def async_stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        await self.client.close()

    async def _connection_loop(self) -> None:
        delay = BACKOFF_INITIAL
        while True:
            started = time.monotonic()
            await self._connect_once()
            if time.monotonic() - started >= STABLE_CONNECTION:
                delay = BACKOFF_INITIAL
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    async def _connect_any(self) -> str | None:
        """Try each address in order; return the one that connected."""
        ctx = await self._ssl_context()
        for address in self.addresses:
            host, port = split_address(address, self.port)
            try:
                await self.client.connect(host, port, ssl_context=ctx)
            except BriaTLSError as err:
                self._log_failure("TLS verification failed for Bria %s at %s: %s", self.name, address, err)
                self._raise_tls_issue(address, str(err))
            except BriaConnectionError as err:
                self._log_failure("Cannot connect to Bria %s at %s:%s: %s", self.name, host, port, err)
            else:
                return address
        return None

    async def _connect_once(self) -> None:
        """Connect, refresh everything, read until the connection drops."""
        if (address := await self._connect_any()) is None:
            return

        if self._failures:
            _LOGGER.info("Reconnected to Bria %s at %s after %d failed attempts", self.name, address, self._failures)
        else:
            _LOGGER.debug("Connected to Bria %s at %s", self.name, address)
        self._failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

        self.address = address
        self.data["connected"] = True
        self._notify()
        reader = asyncio.create_task(self.client.run())
        try:
            await self._refresh_all()
            await reader
        finally:
            if not reader.done():
                reader.cancel()
            self.address = None
            self.data = _default_data()
            self._notify()
            _LOGGER.debug("Disconnected from Bria %s at %s", self.name, address)

    def _log_failure(self, msg: str, *args: Any) -> None:
        self._failures += 1
        level = logging.WARNING if self._failures == 1 else logging.DEBUG
        _LOGGER.log(level, msg, *args)

    def _raise_tls_issue(self, address: str, error: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="tls_verification_failed",
            translation_placeholders={"host": f"{self.name} ({address})", "error": error},
        )

    # -- status refreshes -----------------------------------------------------

    async def _refresh_all(self) -> None:
        await asyncio.gather(
            *(fn() for fn in self._refreshers().values() if fn is not None),
            self._refresh_accounts(),
        )

    def _refreshers(self) -> dict[str, Callable[[], Awaitable[None]]]:
        """Event type (lower case) -> refresh coroutine."""
        return {
            STATUS_AUTHENTICATION.lower(): self._refresh_authentication,
            STATUS_CALL.lower(): self._refresh_calls,
            STATUS_PRESENCE.lower(): self._refresh_presence,
            STATUS_PHONE.lower(): self._refresh_phone,
            STATUS_SYSTEM_INFO.lower(): self._refresh_system_info,
            STATUS_SYSTEM_SETTINGS.lower(): self._refresh_system_settings,
            STATUS_AUDIO_DEVICES.lower(): self._refresh_audio_devices,
            STATUS_AUDIO_PROPERTIES.lower(): self._refresh_audio_properties,
            STATUS_CALL_OPTIONS.lower(): self._refresh_call_options,
            STATUS_MISSED_CALL.lower(): self._refresh_missed_calls,
            STATUS_VOICEMAIL.lower(): self._refresh_voicemail,
            STATUS_CALLHISTORY.lower(): self._refresh_history,
            STATUS_SCREENSHARE.lower(): self._refresh_screenshare,
            STATUS_SUPPORTED_PRESENCE.lower(): self._refresh_supported_presence,
        }

    @callback
    def _on_event(self, event_type: str) -> None:
        etype = event_type.lower()
        if etype == STATUS_ACCOUNT:
            refresh: Callable[[], Awaitable[None]] | None = self._refresh_accounts
        else:
            refresh = self._refreshers().get(etype)
        if refresh is None:
            _LOGGER.debug("Ignoring Bria event %s", event_type)
            return
        if etype in (STATUS_CALL.lower(), STATUS_PRESENCE.lower()):
            # Someone is using this computer; it becomes the "last used" machine.
            self.last_activity = time.monotonic()
        self.entry.async_create_background_task(
            self.hass, refresh(), f"{DOMAIN} refresh {etype}"
        )

    async def _fetch(self, status_type: str, *extra: ET.Element) -> ET.Element | None:
        """Request a status; None on any Bria error (logged at debug)."""
        try:
            resp = await self.client.status(status_type, *extra)
        except BriaError as err:
            _LOGGER.debug("Status %s failed: %s", status_type, err)
            return None
        return resp.root

    def _apply(self, **values: Any) -> None:
        if not self.client.connected:
            return  # a late reply after disconnect must not resurrect state
        self.data.update(values)
        self._notify()

    async def _refresh_authentication(self) -> None:
        if (root := await self._fetch(STATUS_AUTHENTICATION)) is not None:
            self._apply(
                authenticated=bool(_bool(root.findtext("authenticated"))),
                auth_reason=_text(root, "notAuthenticatedReason"),
                auth_server_reason=_text(root, "serverProvidedReason"),
            )

    async def _refresh_calls(self) -> None:
        if (root := await self._fetch(STATUS_CALL)) is not None:
            self._apply(active_calls=parse_calls(root))

    async def _refresh_presence(self) -> None:
        if (root := await self._fetch(STATUS_PRESENCE)) is not None:
            self._apply(
                presence_status=_text(root, "presenceStatus"),
                presence_text=_text(root, "presenceText"),
            )

    async def _refresh_supported_presence(self) -> None:
        if (root := await self._fetch(STATUS_SUPPORTED_PRESENCE)) is not None:
            self._apply(
                supported_presences=[
                    el.text.strip() for el in root.findall("presenceStatus") if el.text
                ]
            )

    async def _refresh_phone(self) -> None:
        if (root := await self._fetch(STATUS_PHONE)) is not None:
            self._apply(
                phone_state=_text(root, "state"),
                phone_call=_text(root, "call"),
                phone_max_lines=_int(root.findtext("maxLines")),
                phone_account_status=_text(root, "accountStatus"),
                phone_account_failure_code=_text(root, "accountFailureCode"),
            )

    async def _refresh_system_info(self) -> None:
        if (root := await self._fetch(STATUS_SYSTEM_INFO)) is not None:
            self._apply(
                system_company_name=_text(root, "systemCompanyName"),
                system_product_name=_text(root, "systemProductName"),
                system_product_version=_text(root, "systemProductVersion"),
                system_product_build=_text(root, "systemProductBuild"),
            )

    async def _refresh_system_settings(self) -> None:
        if (root := await self._fetch(STATUS_SYSTEM_SETTINGS)) is not None:
            self._apply(
                system_default_call_type=_text(root, "defaultCallType"),
                system_call_right_away=_bool(root.findtext("callRightAwayOnceNumberSelected")),
            )

    async def _refresh_audio_devices(self) -> None:
        # Bria 6.8 answers this with <status type="deviceConfiguration">, so the
        # reply is matched by transaction, never by its type attribute.
        if (root := await self._fetch(STATUS_AUDIO_DEVICES)) is not None:
            self._apply(audio_devices=parse_audio_devices(root))

    async def _refresh_audio_properties(self) -> None:
        if (root := await self._fetch(STATUS_AUDIO_PROPERTIES)) is None:
            return
        values: dict[str, Any] = {
            "audio_mute": _text(root, "mute"),
            "audio_speaker_mute": _text(root, "speakerMute"),
            "audio_speaker": _text(root, "speaker"),
        }
        for vol in root.findall("volume"):
            key = {"speaker": "audio_speaker_volume", "microphone": "audio_microphone_volume"}.get(
                vol.get("type", "")
            )
            if key:
                values[key] = _int(vol.text)
        self._apply(**values)

    async def _refresh_call_options(self) -> None:
        if (root := await self._fetch(STATUS_CALL_OPTIONS)) is not None:
            self._apply(
                call_option_anonymous=_bool(root.findtext("anonymous")),
                call_option_letters_to_numbers=_bool(root.findtext("lettersToNumbers")),
                call_option_auto_answer=_bool(root.findtext("autoAnswer")),
                call_option_call_waiting=_bool(root.findtext("callWaiting")),
            )

    async def _refresh_missed_calls(self) -> None:
        # NOTE: per the API guide (p. 50-51) this query may reset Bria's own
        # missed-call counter. Pending live verification (PLAN V3).
        if (root := await self._fetch(STATUS_MISSED_CALL)) is not None:
            self._apply(missed_call_count=_int(root.findtext("count")))

    async def _refresh_voicemail(self) -> None:
        if (root := await self._fetch(STATUS_VOICEMAIL)) is not None:
            self._apply(
                voicemail_accounts=[
                    {
                        "account_id": _text(vm, "accountId") or "",
                        "account_name": _text(vm, "accountName") or "",
                        "count": _int(vm.findtext("count")),
                    }
                    for vm in root.findall("voiceMail")
                ]
            )

    async def _refresh_accounts(self) -> None:
        sip, xmpp = await asyncio.gather(
            self._fetch(STATUS_ACCOUNT, xml_el("accountType", "sip")),
            self._fetch(STATUS_ACCOUNT, xml_el("accountType", "xmpp")),
        )
        values: dict[str, Any] = {}
        if sip is not None:
            values["sip_accounts"] = parse_accounts(sip)
        if xmpp is not None:
            values["xmpp_accounts"] = parse_accounts(xmpp)
        if values:
            self._apply(**values)

    async def _refresh_history(self) -> None:
        root = await self._fetch(
            STATUS_CALLHISTORY, xml_el("count", HISTORY_COUNT), xml_el("entryType", "all")
        )
        if root is not None:
            self._apply(call_history=parse_history(root))

    async def _refresh_screenshare(self) -> None:
        if self.data.get("screen_share_supported") is False:
            return
        try:
            resp = await self.client.status(STATUS_SCREENSHARE)
        except BriaBadRequest:
            _LOGGER.info("Bria %s does not support screen sharing", self.name)
            self._apply(screen_share_supported=False)
            return
        except BriaError as err:
            _LOGGER.debug("Status screenShare failed: %s", err)
            return
        session = resp.root.find("session") if resp.root is not None else None
        self._apply(
            screen_share_supported=True,
            screen_share_status=(_text(session, "status") or "inactive") if session is not None else "inactive",
            screen_share_url=_text(session, "joinUrl") if session is not None else None,
        )

    # -- actions ------------------------------------------------------------------

    async def _action(self, path: str, body: ET.Element | None = None) -> BriaResponse:
        try:
            return await self.client.request(path, body)
        except BriaError as err:
            raise HomeAssistantError(f"Bria {path} failed: {err}") from err

    async def async_set_presence(self, status: str, text: str = "") -> None:
        await self._action(
            "setPresence",
            xml_el("presence", None, xml_el("presenceStatus", status), xml_el("presenceText", text)),
        )
        await self._refresh_presence()

    async def async_place_call(
        self,
        number: str,
        suppress_gui: bool = False,
        *,
        call_type: str = "audio",
        display_name: str | None = None,
        account_id: str | None = None,
    ) -> None:
        await self._action(
            "call",
            xml_el(
                "dial",
                None,
                xml_el("number", number),
                xml_el("displayName", display_name or ""),
                xml_el("suppressMainWindow", suppress_gui),
                xml_el("accountId", account_id) if account_id else None,
                type=call_type,
            ),
        )

    async def async_answer_call(self, call_id: str, with_video: bool = False) -> None:
        await self._action(
            "answer", xml_el("answerCall", None, xml_el("callId", call_id), xml_el("withVideo", with_video))
        )

    async def async_end_call(self, call_id: str) -> None:
        await self._action("endCall", xml_el("endCall", None, xml_el("callId", call_id)))

    async def async_hold_call(self, call_id: str) -> None:
        await self._action("hold", xml_el("holdCall", None, xml_el("callId", call_id)))

    async def async_resume_call(self, call_id: str) -> None:
        await self._action("resume", xml_el("resumeCall", None, xml_el("callId", call_id)))

    async def async_transfer_call(
        self, call_id: str, target_number: str | None = None, target_call_id: str | None = None
    ) -> None:
        if target_call_id:
            target = xml_el("targetCallId", target_call_id)
        elif target_number:
            # Bria before 6.2.0 expects <target> instead of <targetNumber> (p. 38).
            legacy = version_tuple(self.data.get("system_product_version")) < (6, 2)
            target = xml_el("target" if legacy and self.data.get("system_product_version") else "targetNumber", target_number)
        else:
            raise HomeAssistantError("transfer_call needs target_number or target_call_id")
        await self._action("transferCall", xml_el("transferCall", None, xml_el("callId", call_id), target))

    async def async_bring_to_front(self, window: str = "main") -> None:
        await self._action("bringToFront", xml_el("bringToFront", None, xml_el("window", window)))

    async def async_start_attended_transfer(self, call_id: str, target_number: str) -> None:
        await self._action(
            "startAttendedTransferCall",
            xml_el(
                "startAttendedTransferCall",
                None,
                xml_el("callId", call_id),
                xml_el("targetNumber", target_number),
            ),
        )

    async def async_merge_calls(self, call_id: str) -> None:
        await self._action("merge", xml_el("merge", None, xml_el("callId", call_id)))

    async def async_set_call_options(
        self,
        anonymous: str | None = None,
        letters_to_numbers: str | None = None,
        auto_answer: str | None = None,
        call_waiting: str | None = None,
    ) -> None:
        opts = {
            "anonymous": anonymous,
            "lettersToNumbers": letters_to_numbers,
            "autoAnswer": auto_answer,
            "callWaiting": call_waiting,
        }
        await self._action(
            "callOptions",
            xml_el("callOptions", None, *(xml_el(k, v) for k, v in opts.items() if v is not None)),
        )
        await self._refresh_call_options()

    async def async_check_voicemail(self, account_id: str, suppress_gui: bool = True) -> None:
        await self._action(
            "checkVoiceMail",
            xml_el(
                "checkVoiceMail",
                None,
                xml_el("accountId", account_id),
                xml_el("suppressMainWindow", suppress_gui),
            ),
        )

    async def async_get_contact(self, email: str) -> dict[str, Any]:
        """Look up a contact by email. Fires bria_contact_lookup and returns the result."""
        try:
            resp = await self.client.status("contact", xml_el("email", email))
        except BriaError as err:
            raise HomeAssistantError(f"Bria contact lookup failed: {err}") from err
        contact = parse_contact(resp.root)
        result: dict[str, Any] = (
            {"found": True, **contact} if contact else {"found": False, "email": email}
        )
        self.hass.bus.async_fire(f"{DOMAIN}_contact_lookup", result)
        return result

    async def async_sign_in(self, username: str, password: str) -> None:
        await self._action("signIn", xml_el("signIn", None, xml_el("user", username), xml_el("password", password)))

    async def async_sign_out(self) -> None:
        await self._action("signOut")

    async def async_set_audio_properties(
        self,
        mute: str | None = None,
        speaker_mute: str | None = None,
        speaker: str | None = None,
        speaker_volume: int | None = None,
        microphone_volume: int | None = None,
    ) -> None:
        children = [
            xml_el("mute", mute) if mute is not None else None,
            xml_el("speakerMute", speaker_mute) if speaker_mute is not None else None,
            xml_el("speaker", speaker) if speaker is not None else None,
            xml_el("volume", speaker_volume, type="speaker") if speaker_volume is not None else None,
            xml_el("volume", microphone_volume, type="microphone")
            if microphone_volume is not None
            else None,
        ]
        await self._action("audioProperties", xml_el("audioProperties", None, *children))
        await self._refresh_audio_properties()

    async def async_select_audio_devices(self, devices: list[dict[str, str]]) -> None:
        await self._action(
            "selectAudioDevices",
            xml_el(
                "devices",
                None,
                *(
                    xml_el(
                        "device",
                        None,
                        xml_el("name", d["name"]),
                        xml_el("id", d["id"]),
                        xml_el("type", d["type"]),
                        xml_el("role", d["role"]),
                    )
                    for d in devices
                ),
            ),
        )
        await self._refresh_audio_devices()

    async def async_exit(self) -> None:
        await self._action("exit")

    async def async_send_dtmf(self, digit: str, tone: str = "start") -> None:
        await self._action("dtmf", xml_el("dtmf", None, xml_el("tone", tone, digit=digit)))

    async def async_start_call_recording(
        self, filename: str, call_id: str | None = None, suppress_popup: bool = True
    ) -> None:
        await self._action(
            "startCallRecording",
            xml_el(
                "startCallRecording",
                None,
                xml_el("callId", call_id) if call_id else None,
                xml_el("filename", filename),
                xml_el("suppressPopup", suppress_popup),
            ),
        )

    async def async_stop_call_recording(self, call_id: str | None = None) -> None:
        await self._action(
            "stopCallRecording",
            xml_el("stopCallRecording", None, xml_el("callId", call_id) if call_id else None),
        )

    async def async_show_history(self, history_type: str = "all", text: str = "") -> None:
        await self._action(
            "showHistory",
            xml_el("filter", None, xml_el("type", history_type), xml_el("text", text) if text else None),
        )

    async def async_get_call_history(self, count: int = 10, entry_type: str = "all") -> list[dict[str, Any]]:
        """Fetch history without touching the Call History sensor. Fires bria_call_history."""
        try:
            resp = await self.client.status(
                STATUS_CALLHISTORY, xml_el("count", count), xml_el("entryType", entry_type)
            )
        except BriaError as err:
            raise HomeAssistantError(f"Bria call history failed: {err}") from err
        entries = parse_history(resp.root) if resp.root is not None else []
        self.hass.bus.async_fire(f"{DOMAIN}_call_history", {"entries": _jsonable(entries)})
        return entries

    async def async_get_call_history_item(self, item_id: str) -> list[dict[str, Any]]:
        try:
            resp = await self.client.status("callHistoryItem", xml_el("id", item_id))
        except BriaError as err:
            raise HomeAssistantError(f"Bria call history item failed: {err}") from err
        entries = parse_history(resp.root) if resp.root is not None else []
        self.hass.bus.async_fire(f"{DOMAIN}_call_history", {"entries": _jsonable(entries)})
        return entries

    async def async_start_im(self, address: str, im_type: str = "sip") -> None:
        await self._action("im", xml_el("im", None, xml_el("address", address), type=im_type))

    async def async_start_screen_share(self, invitees: list[dict[str, str]]) -> None:
        await self._action(
            "startScreenShare",
            xml_el(
                "invitees",
                None,
                *(
                    xml_el(
                        "invitee",
                        None,
                        xml_el("address", inv.get("address", ""), type=inv.get("type", "simple")),
                    )
                    for inv in invitees
                ),
            ),
        )

    async def async_start_collab(self) -> None:
        await self._action("startCollab")

    async def async_join_collab(self, collab_url: str) -> None:
        await self._action("joinCollab", xml_el("joinCollab", None, xml_el("collabUrl", collab_url)))


def _jsonable(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Event payloads must be JSON-serialisable: datetimes become ISO strings."""
    return [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items()}
        for e in entries
    ]
