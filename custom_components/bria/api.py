"""Bria Desktop API client: WebSocket transport, envelope and request correlation.

This module knows nothing about Home Assistant entities. It owns one WebSocket
connection to one Bria instance, sends requests in Bria's HTTP-like envelope,
matches responses to requests by ``Transaction-ID`` and hands ``statusChange``
events to a callback.

Wire format: Bria Desktop API Developer's Guide (2022-03-22), pp. 2-4, 12-18.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import ipaddress
import itertools
import logging
import ssl
from typing import Any
import xml.etree.ElementTree as ET

import aiohttp
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

_LOGGER = logging.getLogger(__name__)

# Bria's embedded certificate is issued for this name. It resolves publicly to
# 127.0.0.1, so for a remote Bria we connect to the real address and present
# this name in TLS SNI and the Host header (verified against Bria 6.8.7).
BRIA_HOSTNAME = "cpclientapi.softphone.com"
WS_PATH = "/counterpath/socketapi/v1/"
XML_DECLARATION = '<?xml version="1.0" encoding="utf-8" ?>'

DEFAULT_REQUEST_TIMEOUT = 10.0
CONNECT_TIMEOUT = 5.0
HEARTBEAT = 30.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BriaError(Exception):
    """Base error for the Bria API."""


class BriaNotConnected(BriaError):
    """No open connection to Bria."""


class BriaTimeout(BriaError):
    """Bria did not answer in time."""


class BriaBadRequest(BriaError):
    """Bria answered 400: it did not understand the request (or does not support it)."""


class BriaNotReady(BriaError):
    """Bria answered 503: it is in the notReady or notAllowed state (p. 18)."""


class BriaConnectionError(BriaError):
    """Could not open a connection."""


class BriaTLSError(BriaConnectionError):
    """TLS handshake or certificate verification failed."""


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def xml_el(
    tag: str,
    text: str | int | bool | None = None,
    *children: ET.Element | None,
    **attrib: str,
) -> ET.Element:
    """Build an element. Text and attributes are escaped by ElementTree.

    Booleans become ``true``/``false``; ``None`` children are skipped, which makes
    optional fields easy: ``xml_el("dial", None, xml_el("number", n), opt and xml_el(...))``.
    """
    el = ET.Element(tag, {k: str(v) for k, v in attrib.items()})
    if text is not None:
        el.text = str(text).lower() if isinstance(text, bool) else str(text)
    for child in children:
        if child is not None:
            el.append(child)
    return el


def to_body(root: ET.Element | None) -> str:
    """Serialise a request body with the declaration Bria expects."""
    if root is None:
        return ""
    return XML_DECLARATION + ET.tostring(root, encoding="unicode")


def parse_xml(text: str) -> ET.Element | None:
    """Parse untrusted XML from Bria. Returns None for empty or unparsable input."""
    text = text.strip()
    if not text:
        return None
    try:
        return SafeET.fromstring(text)
    except (ET.ParseError, DefusedXmlException):
        # Some Bria builds emit stray text before the declaration; retry after it.
        if "?>" in text:
            try:
                return SafeET.fromstring(text.split("?>", 1)[1].strip())
            except (ET.ParseError, DefusedXmlException):
                pass
    _LOGGER.debug("Unparsable XML from Bria: %.200s", text)
    return None


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    """One parsed message from Bria."""

    kind: str  # "response", "event" or "unknown"
    code: int | None = None  # HTTP-style status for responses
    path: str | None = None  # for events, e.g. "statusChange"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def transaction_id(self) -> str | None:
        return self.headers.get("transaction-id")


def parse_frame(raw: str) -> Frame:
    """Parse Bria's HTTP-like envelope.

    Responses start ``HTTP/1.1 <code> <reason>``; events start ``POST /<path>``.
    Headers follow, then a blank line and the XML body. Some frames omit the
    blank line, so the body also starts at the first line beginning with ``<``.
    """
    lines = raw.replace("\r\n", "\n").split("\n")
    first = lines[0].strip()
    frame = Frame(kind="unknown")
    if first.startswith("HTTP/"):
        parts = first.split(maxsplit=2)
        try:
            frame.code = int(parts[1])
            frame.kind = "response"
        except (IndexError, ValueError):
            pass
    elif first.startswith("POST"):
        frame.kind = "event"
        frame.path = first[4:].strip().lstrip("/") or None

    body_start = len(lines)
    for i, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if not stripped:
            body_start = i + 1
            break
        if stripped.startswith("<"):
            body_start = i
            break
        key, sep, value = line.partition(":")
        if sep:
            frame.headers[key.strip().lower()] = value.strip()
    frame.body = "\n".join(lines[body_start:]).strip()
    return frame


def build_request(path: str, transaction_id: str, body: str, user_agent: str) -> str:
    """Build a request frame (p. 18)."""
    msg = (
        f"GET /{path}\r\n"
        f"User-Agent: {user_agent}\r\n"
        f"Transaction-ID: {transaction_id}\r\n"
        "Content-Type: application/xml\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}"
    )
    if body:
        msg += f"\r\n\r\n{body}"
    return msg


def event_type(frame: Frame) -> str | None:
    """Return the ``type`` of a statusChange event, or None."""
    root = parse_xml(frame.body)
    if root is None:
        return None
    el = root if root.tag == "event" else root.find("event")
    return (el if el is not None else root).get("type")


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BriaResponse:
    """A response to one request."""

    code: int
    root: ET.Element | None

    @property
    def empty(self) -> bool:
        """True for 204 No Content or a 200 with no body."""
        return self.root is None


def ws_url(address: str, port: int, secure: bool = True) -> str:
    """Build the WebSocket URL for an address (IPv4, IPv6 or hostname)."""
    host = address
    try:
        if ipaddress.ip_address(address).version == 6:
            host = f"[{address}]"
    except ValueError:
        pass
    return f"{'wss' if secure else 'ws'}://{host}:{port}{WS_PATH}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BriaClient:
    """One WebSocket connection to one Bria instance.

    Usage::

        client = BriaClient(session, on_event=cb)
        await client.connect("192.168.1.50", 9002, ssl_context=ctx)
        task = asyncio.create_task(client.run())   # reads until the socket closes
        resp = await client.request("status", xml_el("status", None, xml_el("type", "phone")))
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        on_event: Callable[[str], None] | None = None,
        user_agent: str = "HomeAssistant-Bria",
    ) -> None:
        self._session = session
        self._on_event = on_event
        self._user_agent = user_agent
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[BriaResponse]] = {}
        self._tid = itertools.count(1)
        self.address: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(
        self,
        address: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        """Open the WebSocket. ``ssl_context=None`` means plain ``ws://`` (tests only)."""
        secure = ssl_context is not None
        kwargs: dict[str, Any] = {
            # Read at call time so tests can disable it: aiohttp can re-arm the
            # heartbeat timer when data arrives during close, which trips the
            # HA test harness's lingering-timer check.
            "heartbeat": HEARTBEAT,
            "timeout": aiohttp.ClientWSTimeout(ws_close=10),
            "autoping": True,
        }
        if secure:
            kwargs["ssl"] = ssl_context
            kwargs["server_hostname"] = BRIA_HOSTNAME
            kwargs["headers"] = {"Host": f"{BRIA_HOSTNAME}:{port}"}
        try:
            async with asyncio.timeout(timeout):
                self._ws = await self._session.ws_connect(
                    ws_url(address, port, secure), **kwargs
                )
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError, ssl.SSLError) as err:
            raise BriaTLSError(str(err)) from err
        except TimeoutError as err:
            raise BriaConnectionError(f"timed out connecting to {address}:{port}") from err
        except (aiohttp.ClientError, OSError) as err:
            raise BriaConnectionError(str(err)) from err
        self.address = address

    async def run(self) -> None:
        """Read frames until the connection closes. Always fails pending requests on exit."""
        ws = self._ws
        if ws is None:
            raise BriaNotConnected
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._fail_pending(BriaNotConnected("connection closed"))
            if not ws.closed:
                await ws.close()
            self._ws = None

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
        self._fail_pending(BriaNotConnected("connection closed"))

    async def request(
        self,
        path: str,
        body: ET.Element | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> BriaResponse:
        """Send one request and wait for its response.

        Raises BriaNotConnected, BriaTimeout, BriaBadRequest (400) or BriaNotReady (503).
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise BriaNotConnected
        tid = str(next(self._tid))
        future: asyncio.Future[BriaResponse] = asyncio.get_running_loop().create_future()
        self._pending[tid] = future
        try:
            await ws.send_str(build_request(path, tid, to_body(body), self._user_agent))
            async with asyncio.timeout(timeout):
                response = await future
        except TimeoutError as err:
            raise BriaTimeout(f"no response to {path}") from err
        except (aiohttp.ClientError, ConnectionError) as err:
            raise BriaNotConnected(str(err)) from err
        finally:
            self._pending.pop(tid, None)

        if response.code == 400:
            raise BriaBadRequest(f"Bria rejected {path} (400 Bad Request)")
        if response.code == 503:
            raise BriaNotReady(f"Bria is not ready for {path} (503 Service Unavailable)")
        if response.code >= 400:
            raise BriaError(f"Bria answered {response.code} to {path}")
        return response

    async def status(
        self, status_type: str, *extra: ET.Element | None, timeout: float = DEFAULT_REQUEST_TIMEOUT
    ) -> BriaResponse:
        """``GET /status`` with ``<type>status_type</type>`` and optional extra elements."""
        return await self.request(
            "status", xml_el("status", None, xml_el("type", status_type), *extra), timeout=timeout
        )

    # -- internals ---------------------------------------------------------

    def _dispatch(self, raw: str) -> None:
        try:
            frame = parse_frame(raw)
            if frame.kind == "response":
                tid = frame.transaction_id
                future = self._pending.get(tid) if tid else None
                if future is None:
                    _LOGGER.debug("Unmatched Bria response (tid=%s): %.120s", tid, raw)
                    return
                if not future.done():
                    future.set_result(BriaResponse(frame.code or 0, parse_xml(frame.body)))
            elif frame.kind == "event":
                etype = event_type(frame)
                if etype and self._on_event:
                    self._on_event(etype)
            else:
                _LOGGER.debug("Unrecognised Bria frame: %.120s", raw)
        except Exception:  # noqa: BLE001 - a bad frame must never drop the connection
            _LOGGER.exception("Error handling Bria frame: %.200s", raw)

    def _fail_pending(self, err: BriaError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(err)
        self._pending.clear()
