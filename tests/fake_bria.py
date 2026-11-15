"""A scriptable fake of the Bria Desktop API WebSocket server.

Speaks the HTTP-like envelope from the Bria Desktop API guide (pp. 3, 12-18):

* client -> server:  ``GET /<path>`` + headers + optional XML body
* server -> client:  ``HTTP/1.1 <code> <reason>`` response, echoing Transaction-ID
* server -> client:  ``POST /statusChange`` event with ``<event type="..."/>``

Tests script it by filling ``status_responses`` (keyed by the ``<type>`` of a
``GET /status`` request) and ``action_responses`` (keyed by request path), then
inspect ``requests`` and call ``push_event``.

Plain ``ws://`` over localhost. TLS handling is tested separately.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

WS_PATH = "/counterpath/socketapi/v1/"
XML_DECL = '<?xml version="1.0" encoding="utf-8" ?>\n'


@dataclass
class FakeRequest:
    """One request received from the integration."""

    path: str
    headers: dict[str, str]
    body: str

    @property
    def root(self) -> ET.Element | None:
        if not self.body.strip():
            return None
        return ET.fromstring(self.body.split("?>", 1)[-1] if "?>" in self.body else self.body)

    @property
    def status_type(self) -> str | None:
        root = self.root
        if self.path != "status" or root is None:
            return None
        return root.findtext("type")


@dataclass
class Reply:
    """A scripted reply: status code and optional XML body (without declaration)."""

    code: int = 200
    body: str = ""


_REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 503: "Service Unavailable"}


def parse_request(raw: str) -> FakeRequest:
    """Parse one client frame into a FakeRequest."""
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    method, _, path = lines[0].partition(" ")
    assert method == "GET", f"unexpected method in {lines[0]!r}"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return FakeRequest(path=path.lstrip("/"), headers=headers, body=body)


def build_response(reply: Reply, transaction_id: str | None) -> str:
    """Build a response frame."""
    body = f"{XML_DECL}{reply.body}" if reply.body else ""
    lines = [f"HTTP/1.1 {reply.code} {_REASONS.get(reply.code, 'Unknown')}"]
    if transaction_id is not None:
        lines.append(f"Transaction-ID: {transaction_id}")
    lines += ["Content-Type: application/xml", f"Content-Length: {len(body.encode())}"]
    frame = "\r\n".join(lines)
    if body:
        frame += f"\r\n\r\n{body}"
    return frame


def build_event(event_type: str) -> str:
    """Build a POST /statusChange event frame."""
    body = f'{XML_DECL}<event type="{event_type}"></event>'
    return (
        "POST /statusChange\r\n"
        "User-Agent: Bria\r\n"
        "Content-Type: application/xml\r\n"
        f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
    )


@dataclass
class FakeBria:
    """Fake Bria server state. Start with ``await fake.start()``."""

    status_responses: dict[str, Reply] = field(default_factory=dict)
    action_responses: dict[str, Reply] = field(default_factory=dict)
    requests: list[FakeRequest] = field(default_factory=list)
    _server: TestServer | None = None
    _sockets: list[web.WebSocketResponse] = field(default_factory=list)
    connected: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.port  # type: ignore[return-value]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{WS_PATH}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(WS_PATH, self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        if self._server is not None:
            await self._server.close()

    async def drop_clients(self) -> None:
        """Close every client connection (simulates Bria quitting)."""
        for ws in list(self._sockets):
            await ws.close()
        self.connected.clear()

    async def push_event(self, event_type: str) -> None:
        for ws in self._sockets:
            await ws.send_str(build_event(event_type))

    def requests_for(self, path: str) -> list[FakeRequest]:
        return [r for r in self.requests if r.path == path]

    def status_types_requested(self) -> list[str | None]:
        return [r.status_type for r in self.requests if r.path == "status"]

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.append(ws)
        self.connected.set()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                req = parse_request(msg.data)
                self.requests.append(req)
                if req.path == "status":
                    reply = self.status_responses.get(req.status_type or "", Reply(200))
                else:
                    reply = self.action_responses.get(req.path, Reply(200))
                await ws.send_str(build_response(reply, req.headers.get("transaction-id")))
        finally:
            self._sockets.remove(ws)
        return ws


def default_status_responses() -> dict[str, Reply]:
    """Status replies for a ready, idle Bria, using examples from the API guide."""
    return {
        "authentication": Reply(
            body='<status type="authentication"><authenticated>true</authenticated></status>'
        ),
        "call": Reply(body='<status type="call"></status>'),
        "phone": Reply(
            body=(
                '<status type="phone"><state>ready</state><call>allow</call>'
                "<maxLines>6</maxLines><accountStatus>connected</accountStatus>"
                "<accountFailureCode>0</accountFailureCode></status>"
            )
        ),
        "presence": Reply(
            body=(
                '<status type="presence"><presenceStatus>available</presenceStatus>'
                "<presenceText>at work</presenceText></status>"
            )
        ),
        "systemInformation": Reply(
            body=(
                '<status type="systemInformation">'
                "<systemCompanyName>CounterPath Corporation</systemCompanyName>"
                "<systemProductName>Bria</systemProductName>"
                "<systemProductVersion>6.5.1</systemProductVersion>"
                "<systemProductBuild>108822</systemProductBuild></status>"
            )
        ),
        "audioProperties": Reply(
            body=(
                '<status type="audioProperties"><mute>disabled</mute>'
                "<speakerMute>disabled</speakerMute><speaker>disabled</speaker>"
                '<volume type="speaker">80</volume><volume type="microphone">50</volume>'
                "</status>"
            )
        ),
        "callOptions": Reply(
            body=(
                '<status type="callOptions"><anonymous>disabled</anonymous>'
                "<lettersToNumbers>disabled</lettersToNumbers>"
                "<autoAnswer>disabled</autoAnswer><callWaiting>enabled</callWaiting>"
                "</status>"
            )
        ),
    }
