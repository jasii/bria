"""Bria WebSocket coordinator — manages connection, state, and actions."""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape as _xe

import aiohttp
import aiohttp.abc

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_HOST,
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
    WS_PATH,
)

_LOGGER = logging.getLogger(__name__)


class _BriaDNSResolver(aiohttp.abc.AbstractResolver):
    """Resolve the Bria API hostname to a specific IP.

    Bria's TLS certificate is issued for ``cpclientapi.softphone.com``.
    When Home Assistant is on a different machine we must connect to the
    user's IP while still presenting that hostname in the TLS SNI so that
    the cert handshake succeeds and Bria shows the permission prompt.
    """

    def __init__(self, target_ip: str) -> None:
        self._target_ip = target_ip

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[dict]:
        return [
            {
                "hostname": host,
                "host": self._target_ip,
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        pass

_DEFAULT_DATA: dict[str, Any] = {
    "presence_status": None,
    "presence_text": None,
    "authenticated": None,
    "auth_reason": None,
    "active_calls": [],
    "screen_share_status": None,
    "screen_share_url": None,
    "phone_state": None,
    "phone_call": None,
    "phone_max_lines": None,
    "phone_account_status": None,
    "phone_account_failure_code": None,
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
    "connected": False,
}


class BriaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages the persistent WebSocket connection to the Bria desktop app."""

    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self._host = host
        self._port = port
        # Always use the Bria API hostname in the URL so TLS SNI matches
        # the certificate embedded in Bria. A custom DNS resolver redirects
        # that hostname to the user-configured IP when needed.
        self._ws_url = f"wss://{DEFAULT_HOST}:{port}{WS_PATH}"
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._transaction_id = 0
        self._reconnect_task: asyncio.Task | None = None
        self.data: dict[str, Any] = dict(_DEFAULT_DATA)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator required override (push model — no polling)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        return self.data

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _next_tid(self) -> str:
        self._transaction_id += 1
        return str(self._transaction_id)

    def _build_message(self, request_type: str, body: str) -> str:
        content_length = len(body.encode("utf-8"))
        msg = (
            f"GET /{request_type}\r\n"
            f"User-Agent: HomeAssistant-Bria\r\n"
            f"Transaction-ID: {self._next_tid()}\r\n"
            f"Content-Type: application/xml\r\n"
            f"Content-Length: {content_length}"
        )
        if content_length > 0:
            msg += f"\r\n\r\n{body}"
        return msg

    async def _send(self, request_type: str, body: str = "") -> None:
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.send_str(self._build_message(request_type, body))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to send message to Bria: %s", err)

    # ------------------------------------------------------------------
    # Status requests
    # ------------------------------------------------------------------

    async def _request_status(self, status_type: str, extra: str = "") -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<status>\n <type>{status_type}</type>\n{extra}</status>"
        )
        await self._send("status", body)

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_message(msg: str) -> dict[str, Any]:
        """Parse the HTTP-like envelope Bria uses over WebSocket."""
        result: dict[str, Any] = {"type": "unknown", "event_type": None, "content": ""}
        lines = msg.replace("\r\n", "\n").split("\n")
        first = lines[0]

        if first.startswith("POST"):
            result["type"] = "event"
            if first[5:].strip().startswith("/statusChange"):
                result["event_type"] = "statusChange"
        elif first.startswith("HTTP/1.1"):
            rest = first[8:].strip()
            if rest.startswith("200 OK"):
                result["type"] = "response"
            elif rest and rest[0] in ("4", "5"):
                result["type"] = "error"

        content_parts: list[str] = []
        in_body = False
        for line in lines[1:]:
            if in_body:
                content_parts.append(line)
            elif line.lstrip().startswith("<"):
                in_body = True
                content_parts.append(line)

        result["content"] = "\n".join(content_parts)
        return result

    @staticmethod
    def _parse_xml(xml_str: str) -> ET.Element | None:
        if not xml_str.strip():
            return None
        try:
            return ET.fromstring(xml_str)
        except ET.ParseError:
            # Strip XML declaration and retry
            if "?>" in xml_str:
                clean = xml_str[xml_str.index("?>") + 2:].strip()
                try:
                    return ET.fromstring(clean)
                except ET.ParseError:
                    pass
        _LOGGER.debug("Could not parse XML: %.200s", xml_str)
        return None

    # ------------------------------------------------------------------
    # Response handlers
    # ------------------------------------------------------------------

    def _handle_response(self, xml_str: str) -> None:
        root = self._parse_xml(xml_str)
        if root is None:
            return
        if root.tag == "status":
            stype = root.attrib.get("type", "")
            handler = {
                STATUS_CALL: self._handle_call_status,
                STATUS_AUTHENTICATION: self._handle_auth_status,
                STATUS_SCREENSHARE: self._handle_screenshare_status,
                STATUS_PRESENCE: self._handle_presence_status,
                STATUS_PHONE: self._handle_phone_status,
                STATUS_SYSTEM_INFO: self._handle_system_info,
                STATUS_SYSTEM_SETTINGS: self._handle_system_settings,
                STATUS_AUDIO_DEVICES: self._handle_audio_devices,
                STATUS_AUDIO_PROPERTIES: self._handle_audio_properties,
                STATUS_CALL_OPTIONS: self._handle_call_options,
                STATUS_MISSED_CALL: self._handle_missed_call,
                STATUS_VOICEMAIL: self._handle_voicemail,
                "contact": self._handle_contact_status,
                STATUS_ACCOUNT: self._handle_account_status,
                STATUS_SUPPORTED_PRESENCE: self._handle_supported_presence,
                STATUS_CALLHISTORY: self._handle_call_history_response,
            }.get(stype)
            if handler:
                handler(root)

    def _handle_presence_status(self, root: ET.Element) -> None:
        self.data["presence_status"] = root.findtext("presenceStatus")
        self.data["presence_text"] = root.findtext("presenceText")

    def _handle_auth_status(self, root: ET.Element) -> None:
        auth = root.findtext("authenticated", "false") or "false"
        self.data["authenticated"] = auth.strip().lower() == "true"
        self.data["auth_reason"] = root.findtext("notAuthenticatedReason")

    def _handle_call_status(self, root: ET.Element) -> None:
        calls: list[dict[str, Any]] = []
        for call in root.findall("call"):
            participants: list[dict[str, str]] = []
            parts_el = call.find("participants")
            if parts_el is not None:
                for p in parts_el.findall("participant"):
                    participants.append(
                        {
                            "number": p.findtext("number", ""),
                            "display_name": p.findtext("displayName", ""),
                            "state": p.findtext("state", ""),
                            "start_time": p.findtext("timeInitiated", ""),
                        }
                    )
            hold_raw = (call.findtext("holdStatus") or "false").strip().lower()
            calls.append(
                {
                    "id": call.findtext("id", ""),
                    "hold": hold_raw == "true",
                    "recording": call.findtext("recordingStatus", ""),
                    "recording_file": call.findtext("recordingFile", ""),
                    "participants": participants,
                }
            )
        self.data["active_calls"] = calls

    def _handle_screenshare_status(self, root: ET.Element) -> None:
        session = root.find("session")
        if session is not None:
            self.data["screen_share_status"] = session.findtext("status", "inactive")
            self.data["screen_share_url"] = session.findtext("joinUrl")
        else:
            self.data["screen_share_status"] = "inactive"
            self.data["screen_share_url"] = None

    def _handle_phone_status(self, root: ET.Element) -> None:
        self.data["phone_state"] = root.findtext("state")
        self.data["phone_call"] = root.findtext("call")
        max_lines = root.findtext("maxLines")
        self.data["phone_max_lines"] = int(max_lines) if max_lines else None
        self.data["phone_account_status"] = root.findtext("accountStatus")
        self.data["phone_account_failure_code"] = root.findtext("accountFailureCode")

    def _handle_system_info(self, root: ET.Element) -> None:
        self.data["system_product_version"] = root.findtext("systemProductVersion")
        self.data["system_product_build"] = root.findtext("systemProductBuild")

    def _handle_system_settings(self, root: ET.Element) -> None:
        self.data["system_default_call_type"] = root.findtext("defaultCallType")
        raw = root.findtext("callRightAwayOnceNumberSelected", "false") or "false"
        self.data["system_call_right_away"] = raw.strip().lower() == "true"

    def _handle_audio_devices(self, root: ET.Element) -> None:
        devices = []
        for device in root.findall(".//device"):
            selected_el = device.find("selected")
            devices.append(
                {
                    "name": device.findtext("name", ""),
                    "id": device.findtext("id", ""),
                    "type": device.findtext("type", ""),
                    "selected": (selected_el.text or "").strip().lower() == "true"
                    if selected_el is not None
                    else False,
                    "role": selected_el.attrib.get("role", "")
                    if selected_el is not None
                    else "",
                }
            )
        self.data["audio_devices"] = devices

    def _handle_audio_properties(self, root: ET.Element) -> None:
        self.data["audio_mute"] = root.findtext("mute")
        self.data["audio_speaker_mute"] = root.findtext("speakerMute")
        self.data["audio_speaker"] = root.findtext("speaker")
        for vol_el in root.findall("volume"):
            vtype = vol_el.attrib.get("type", "")
            try:
                val = int(vol_el.text or "")
            except (ValueError, TypeError):
                val = None
            if vtype == "speaker":
                self.data["audio_speaker_volume"] = val
            elif vtype == "microphone":
                self.data["audio_microphone_volume"] = val

    def _handle_call_options(self, root: ET.Element) -> None:
        def _bool(tag: str) -> bool | None:
            val = root.findtext(tag)
            if val is None:
                return None
            return val.strip().lower() == "enabled"
        self.data["call_option_anonymous"] = _bool("anonymous")
        self.data["call_option_letters_to_numbers"] = _bool("lettersToNumbers")
        self.data["call_option_auto_answer"] = _bool("autoAnswer")
        self.data["call_option_call_waiting"] = _bool("callWaiting")

    def _handle_missed_call(self, root: ET.Element) -> None:
        raw = root.findtext("count")
        try:
            self.data["missed_call_count"] = int(raw) if raw is not None else None
        except (ValueError, TypeError):
            self.data["missed_call_count"] = None

    def _handle_voicemail(self, root: ET.Element) -> None:
        accounts = []
        for vm in root.findall("voiceMail"):
            raw_count = vm.findtext("count")
            try:
                count = int(raw_count) if raw_count is not None else None
            except (ValueError, TypeError):
                count = None
            accounts.append(
                {
                    "account_id": vm.findtext("accountId", ""),
                    "account_name": vm.findtext("accountName", ""),
                    "count": count,
                }
            )
        self.data["voicemail_accounts"] = accounts

    def _handle_account_status(self, root: ET.Element) -> None:
        sip_accounts: list[dict] = []
        xmpp_accounts: list[dict] = []
        for acc in root.findall("account"):
            entry = {
                "account_name": acc.findtext("accountName", ""),
                "account_id": acc.findtext("accountId", ""),
                "type": acc.findtext("type", ""),
                "enabled": (acc.findtext("enabled") or "false").strip().lower() == "true",
                "registered": (acc.findtext("registered") or "false").strip().lower() == "true",
            }
            if entry["type"] == "sip":
                sip_accounts.append(entry)
            elif entry["type"] == "xmpp":
                xmpp_accounts.append(entry)
        if sip_accounts:
            self.data["sip_accounts"] = sip_accounts
        if xmpp_accounts:
            self.data["xmpp_accounts"] = xmpp_accounts

    def _handle_supported_presence(self, root: ET.Element) -> None:
        self.data["supported_presences"] = [
            el.text for el in root.findall("presenceStatus") if el.text
        ]

    def _handle_call_history_response(self, root: ET.Element) -> None:
        history = []
        for entry in root.findall("callHistory"):
            history.append({
                "type": entry.findtext("type", ""),
                "number": entry.findtext("number", ""),
                "display_name": entry.findtext("displayName", ""),
                "account_id": entry.findtext("accountId", ""),
                "duration": entry.findtext("duration"),
                "time_initiated": entry.findtext("timeInitiated"),
                "id": entry.findtext("id", ""),
            })
        self.data["call_history"] = history
        if getattr(self, "_pending_call_history_service", False):
            self._pending_call_history_service = False
            self.hass.bus.async_fire(f"{DOMAIN}_call_history", {"entries": history})

    # ------------------------------------------------------------------
    # Incoming event handling
    # ------------------------------------------------------------------

    def _handle_event(self, xml_str: str) -> None:
        """Handle a statusChange push event from Bria."""
        root = self._parse_xml(xml_str)
        if root is None:
            return
        # XML can be <event type="call"/> directly or wrapped in another element
        event_el = root if root.tag == "event" else root.find("event")
        event_type = (event_el if event_el is not None else root).attrib.get("type", "")

        refresh: dict[str, Any] = {
            STATUS_CALL: lambda: self._request_status(STATUS_CALL),
            STATUS_AUTHENTICATION: lambda: self._request_status(STATUS_AUTHENTICATION),
            STATUS_SCREENSHARE: lambda: self._request_status(STATUS_SCREENSHARE),
            STATUS_PRESENCE: lambda: self._request_status(STATUS_PRESENCE),
            STATUS_PHONE: lambda: self._request_status(STATUS_PHONE),
            STATUS_AUDIO_DEVICES: lambda: self._request_status(STATUS_AUDIO_DEVICES),
            STATUS_AUDIO_PROPERTIES: lambda: self._request_status(STATUS_AUDIO_PROPERTIES),
            STATUS_CALL_OPTIONS: lambda: self._request_status(STATUS_CALL_OPTIONS),
            STATUS_MISSED_CALL: lambda: self._request_status(STATUS_MISSED_CALL),
            # Bria sends statusChange event type as "voicemail" (lowercase)
            "voicemail": lambda: self._request_status(STATUS_VOICEMAIL),
            STATUS_CALLHISTORY: lambda: self._request_status(
                STATUS_CALLHISTORY,
                " <count>50</count>\n <entryType>all</entryType>\n",
            ),
            STATUS_ACCOUNT: lambda: asyncio.gather(
                self._request_status(STATUS_ACCOUNT, " <accountType>sip</accountType>\n"),
                self._request_status(STATUS_ACCOUNT, " <accountType>xmpp</accountType>\n"),
            ),
        }
        if event_type in refresh:
            self.hass.async_create_task(refresh[event_type]())

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def _on_connected(self) -> None:
        self.data["connected"] = True
        await self._request_status(STATUS_AUTHENTICATION)
        await self._request_status(STATUS_CALL)
        await self._request_status(STATUS_SCREENSHARE)
        await self._request_status(STATUS_PRESENCE)
        await self._request_status(STATUS_PHONE)
        await self._request_status(STATUS_SYSTEM_INFO)
        await self._request_status(STATUS_SYSTEM_SETTINGS)
        await self._request_status(STATUS_AUDIO_DEVICES)
        await self._request_status(STATUS_AUDIO_PROPERTIES)
        await self._request_status(STATUS_CALL_OPTIONS)
        await self._request_status(STATUS_MISSED_CALL)
        await self._request_status(STATUS_VOICEMAIL)
        await self._request_status(
            STATUS_CALLHISTORY,
            " <count>50</count>\n <entryType>all</entryType>\n",
        )
        await self._request_status(STATUS_ACCOUNT, " <accountType>sip</accountType>\n")
        await self._request_status(STATUS_ACCOUNT, " <accountType>xmpp</accountType>\n")
        await self._request_status(STATUS_SUPPORTED_PRESENCE)

    async def _listen(self) -> None:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        # If the user configured a specific IP (not the default hostname),
        # use a custom resolver so the URL hostname still carries the correct
        # SNI while the TCP connection goes to the right machine.
        if self._host != DEFAULT_HOST:
            connector = aiohttp.TCPConnector(resolver=_BriaDNSResolver(self._host))
        else:
            connector = aiohttp.TCPConnector()

        try:
            self._session = aiohttp.ClientSession(connector=connector)
            async with self._session.ws_connect(
                self._ws_url, ssl=ssl_ctx
            ) as ws:
                self._ws = ws
                _LOGGER.info(
                    "Connected to Bria (SNI: %s → %s)", DEFAULT_HOST, self._host
                )
                await self._on_connected()
                self.async_set_updated_data(self.data)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        parsed = self._parse_message(msg.data)
                        if parsed["type"] == "response":
                            self._handle_response(parsed["content"])
                            self.async_set_updated_data(self.data)
                        elif parsed["type"] == "event":
                            self._handle_event(parsed["content"])
                        elif parsed["type"] == "error":
                            _LOGGER.warning("Bria API error in message: %.200s", msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
        except aiohttp.ClientConnectorError as err:
            _LOGGER.warning(
                "Cannot connect to Bria at %s (%s) — is Bria running? (%s)",
                self._host,
                self._ws_url,
                err,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected Bria WebSocket error: %s", err)
        finally:
            self._ws = None
            self.data["connected"] = False
            if self._session:
                await self._session.close()
                self._session = None
            self.async_set_updated_data(self.data)

    async def _reconnect_loop(self) -> None:
        while True:
            await self._listen()
            _LOGGER.info("Bria disconnected — retrying in 5 seconds...")
            await asyncio.sleep(5)

    async def async_start(self) -> None:
        """Start the persistent WebSocket listener."""
        self._reconnect_task = self.hass.async_create_task(self._reconnect_loop())

    async def async_stop(self) -> None:
        """Stop the WebSocket listener and clean up."""
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Action methods called by services
    # ------------------------------------------------------------------

    async def async_set_presence(self, status: str, text: str = "") -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<presence>"
            f"<presenceStatus>{_xe(status)}</presenceStatus>"
            f"<presenceText>{_xe(text)}</presenceText>"
            f"</presence>"
        )
        # API requires GET /setPresence, not GET /status
        await self._send("setPresence", body)
        # Explicitly refresh presence so text/status are in sync regardless
        # of whether Bria fires a statusChange event for API-initiated changes.
        await self._request_status(STATUS_PRESENCE)

    async def async_place_call(self, number: str, suppress_gui: bool = False) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f'<dial type="audio">\n'
            f" <number>{_xe(number)}</number>\n"
            f" <displayName></displayName>\n"
            f" <suppressMainWindow>{str(suppress_gui).lower()}</suppressMainWindow>\n"
            f"</dial>"
        )
        await self._send("call", body)

    async def async_answer_call(self, call_id: str, with_video: bool = False) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<answerCall>\n"
            f" <callId>{_xe(call_id)}</callId>\n"
            f" <withVideo>{str(with_video).lower()}</withVideo>\n"
            f"</answerCall>"
        )
        await self._send("answer", body)

    async def async_end_call(self, call_id: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<endCall>\n <callId>{_xe(call_id)}</callId>\n</endCall>"
        )
        await self._send("endCall", body)

    async def async_hold_call(self, call_id: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<holdCall>\n <callId>{_xe(call_id)}</callId>\n</holdCall>"
        )
        await self._send("hold", body)

    async def async_resume_call(self, call_id: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<resumeCall>\n <callId>{_xe(call_id)}</callId>\n</resumeCall>"
        )
        await self._send("resume", body)

    async def async_transfer_call(
        self, call_id: str, target_number: str | None = None, target_call_id: str | None = None
    ) -> None:
        parts = [
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<transferCall>\n <callId>{_xe(call_id)}</callId>\n"
        ]
        if target_call_id:
            parts.append(f" <targetCallId>{_xe(target_call_id)}</targetCallId>\n")
        elif target_number:
            parts.append(f" <targetNumber>{_xe(target_number)}</targetNumber>\n")
        parts.append("</transferCall>")
        await self._send("transferCall", "".join(parts))

    async def async_bring_to_front(self, window: str = "main") -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<bringToFront><window>{_xe(window)}</window></bringToFront>"
        )
        await self._send("bringToFront", body)

    async def async_start_attended_transfer(self, call_id: str, target_number: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<startAttendedTransferCall>\n"
            f" <callId>{_xe(call_id)}</callId>\n"
            f" <targetNumber>{_xe(target_number)}</targetNumber>\n"
            f"</startAttendedTransferCall>"
        )
        await self._send("startAttendedTransferCall", body)

    async def async_merge_calls(self, call_id: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<merge>\n <callId>{_xe(call_id)}</callId>\n</merge>"
        )
        await self._send("merge", body)

    async def async_set_call_options(
        self,
        anonymous: str | None = None,
        letters_to_numbers: str | None = None,
        auto_answer: str | None = None,
        call_waiting: str | None = None,
    ) -> None:
        parts = ["<?xml version='1.0' encoding='UTF-8'?><callOptions>"]
        if anonymous is not None:
            parts.append(f"<anonymous>{anonymous}</anonymous>")
        if letters_to_numbers is not None:
            parts.append(f"<lettersToNumbers>{letters_to_numbers}</lettersToNumbers>")
        if auto_answer is not None:
            parts.append(f"<autoAnswer>{auto_answer}</autoAnswer>")
        if call_waiting is not None:
            parts.append(f"<callWaiting>{call_waiting}</callWaiting>")
        parts.append("</callOptions>")
        await self._send("callOptions", "".join(parts))
        await self._request_status(STATUS_CALL_OPTIONS)

    async def async_check_voicemail(
        self, account_id: str, suppress_gui: bool = True
    ) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<checkVoiceMail>\n"
            f" <accountId>{_xe(account_id)}</accountId>\n"
            f" <suppressMainWindow>{str(suppress_gui).lower()}</suppressMainWindow>\n"
            f"</checkVoiceMail>"
        )
        await self._send("checkVoiceMail", body)

    async def async_get_contact(self, email: str) -> None:
        """Look up a contact by email and fire a HA event with the result."""
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<status>\n <type>contact</type>\n <email>{_xe(email)}</email>\n</status>"
        )
        # Response is handled in _handle_response → dispatched back here via event
        self._pending_contact_email = email
        await self._send("status", body)

    def _handle_contact_status(self, root: ET.Element) -> None:
        contact_el = root.find("contact")
        if contact_el is None:
            self.hass.bus.async_fire(
                f"{DOMAIN}_contact_lookup",
                {"email": getattr(self, "_pending_contact_email", None), "found": False},
            )
            return
        data: dict[str, Any] = {
            "found": True,
            "id": contact_el.findtext("id"),
            "email": contact_el.findtext("email"),
            "presence_status": contact_el.findtext("presenceStatus"),
            "presence_text": contact_el.findtext("presenceText"),
            "presence_address": contact_el.findtext("presenceAddress"),
            "presence_type": contact_el.findtext("presenceType"),
            "collab_url": contact_el.findtext("collabUrl"),
        }
        for tag in ("work", "mobile", "home", "fax", "jid", "other", "softphone"):
            values = [el.text for el in contact_el.findall(tag) if el.text]
            if values:
                data[tag] = values if len(values) > 1 else values[0]
        self.hass.bus.async_fire(f"{DOMAIN}_contact_lookup", data)

    async def async_sign_in(self, username: str, password: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<signIn>\n"
            f" <user>{_xe(username)}</user>\n"
            f" <password>{_xe(password)}</password>\n"
            f"</signIn>"
        )
        await self._send("signIn", body)

    async def async_sign_out(self) -> None:
        await self._send("signOut")

    async def async_set_audio_properties(
        self,
        mute: str | None = None,
        speaker_mute: str | None = None,
        speaker: str | None = None,
        speaker_volume: int | None = None,
        microphone_volume: int | None = None,
    ) -> None:
        parts = ["<?xml version='1.0' encoding='UTF-8'?><audioProperties>"]
        if mute is not None:
            parts.append(f"<mute>{mute}</mute>")
        if speaker_mute is not None:
            parts.append(f"<speakerMute>{speaker_mute}</speakerMute>")
        if speaker is not None:
            parts.append(f"<speaker>{speaker}</speaker>")
        if speaker_volume is not None:
            parts.append(f'<volume type="speaker">{speaker_volume}</volume>')
        if microphone_volume is not None:
            parts.append(f'<volume type="microphone">{microphone_volume}</volume>')
        parts.append("</audioProperties>")
        await self._send("audioProperties", "".join(parts))
        await self._request_status(STATUS_AUDIO_PROPERTIES)

    async def async_select_audio_devices(
        self, devices: list[dict[str, str]]
    ) -> None:
        parts = ["<?xml version='1.0' encoding='UTF-8'?><devices>"]
        for d in devices:
            parts.append(
                f" <device>"
                f"<name>{d['name']}</name>"
                f"<id>{d['id']}</id>"
                f"<type>{d['type']}</type>"
                f"<role>{d['role']}</role>"
                f"</device>"
            )
        parts.append("</devices>")
        await self._send("selectAudioDevices", "".join(parts))
        await self._request_status(STATUS_AUDIO_DEVICES)

    async def async_exit(self) -> None:
        """Exit the Bria application."""
        await self._send("exit")

    async def async_send_dtmf(self, digit: str, tone: str = "start") -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f'<dtmf>\n <tone digit="{_xe(digit)}">{_xe(tone)}</tone>\n</dtmf>'
        )
        await self._send("dtmf", body)

    async def async_start_call_recording(
        self, filename: str, call_id: str | None = None, suppress_popup: bool = True
    ) -> None:
        parts = [
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<startCallRecording>\n"
        ]
        if call_id:
            parts.append(f" <callId>{_xe(call_id)}</callId>\n")
        parts.append(f" <filename>{_xe(filename)}</filename>\n")
        parts.append(f" <suppressPopup>{str(suppress_popup).lower()}</suppressPopup>\n")
        parts.append("</startCallRecording>")
        await self._send("startCallRecording", "".join(parts))

    async def async_stop_call_recording(self, call_id: str | None = None) -> None:
        if call_id:
            body = (
                "<?xml version='1.0' encoding='UTF-8'?>"
                f"<stopCallRecording>\n <callId>{_xe(call_id)}</callId>\n</stopCallRecording>"
            )
        else:
            body = (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<stopCallRecording/>"
            )
        await self._send("stopCallRecording", body)

    async def async_show_history(
        self, history_type: str = "all", text: str = ""
    ) -> None:
        parts = [
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<filter>\n"
            f" <type>{_xe(history_type)}</type>\n"
        ]
        if text:
            parts.append(f" <text>{_xe(text)}</text>\n")
        parts.append("</filter>")
        await self._send("showHistory", "".join(parts))

    async def async_get_call_history(
        self, count: int = 10, entry_type: str = "all"
    ) -> None:
        """Request call history and fire a bria_call_history HA event with results."""
        self._pending_call_history_service = True
        await self._request_status(
            "callHistory",
            f" <count>{count}</count>\n <entryType>{entry_type}</entryType>\n",
        )

    async def async_get_call_history_item(self, item_id: str) -> None:
        """Request a single call history entry and fire a bria_call_history HA event."""
        self._pending_call_history_service = True
        await self._request_status(
            "callHistoryItem",
            f" <id>{item_id}</id>\n",
        )

    async def async_start_im(self, address: str, im_type: str = "sip") -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f'<im type="{_xe(im_type)}">\n <address>{_xe(address)}</address>\n</im>'
        )
        await self._send("im", body)

    async def async_start_screen_share(
        self, invitees: list[dict[str, str]]
    ) -> None:
        parts = [
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<invitees>\n"
        ]
        for inv in invitees:
            addr_type = inv.get("type", "simple")
            address = inv.get("address", "")
            parts.append(
                f' <invitee>\n  <address type="{_xe(addr_type)}">{_xe(address)}</address>\n </invitee>\n'
            )
        parts.append("</invitees>")
        await self._send("startScreenShare", "".join(parts))

    async def async_start_collab(self) -> None:
        await self._send("startCollab")

    async def async_join_collab(self, collab_url: str) -> None:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<joinCollab>\n <collabUrl>{_xe(collab_url)}</collabUrl>\n</joinCollab>"
        )
        await self._send("joinCollab", body)
