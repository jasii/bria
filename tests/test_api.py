"""Tests for the Bria protocol client (no Home Assistant entities involved)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.bria.api import (
    BriaBadRequest,
    BriaClient,
    BriaConnectionError,
    BriaNotConnected,
    BriaNotReady,
    BriaTimeout,
    parse_frame,
    to_body,
    ws_url,
    xml_el,
)

from .fake_bria import FakeBria, Reply


@pytest.fixture
async def client(fake_bria: FakeBria) -> AsyncGenerator[tuple[BriaClient, list[str]]]:
    events: list[str] = []
    async with aiohttp.ClientSession() as session:
        c = BriaClient(session, on_event=events.append)
        await c.connect("127.0.0.1", fake_bria.port, ssl_context=None)
        runner = asyncio.create_task(c.run())
        yield c, events
        await c.close()
        await runner


# -- pure helpers -------------------------------------------------------------


def test_xml_escaping() -> None:
    body = to_body(xml_el("dial", None, xml_el("number", "a&b<c>\"'"), xml_el("x", True)))
    assert body.startswith('<?xml version="1.0" encoding="utf-8" ?>')
    assert "<number>a&amp;b&lt;c&gt;\"'</number>" in body
    assert "<x>true</x>" in body


def test_xml_none_children_skipped() -> None:
    assert to_body(xml_el("a", None, None, xml_el("b", 1))).endswith("<a><b>1</b></a>")


def test_parse_frame_response_and_event() -> None:
    f = parse_frame("HTTP/1.1 204 No Content\r\nTransaction-ID: 7\r\nContent-Length: 0")
    assert (f.kind, f.code, f.transaction_id, f.body) == ("response", 204, "7", "")
    e = parse_frame(
        'POST /statusChange\r\nUser-Agent: Bria\r\n\r\n<?xml version="1.0" ?>\n<event type="call"></event>'
    )
    assert (e.kind, e.path) == ("event", "statusChange")
    assert e.body.startswith("<?xml")


def test_parse_frame_body_without_blank_line() -> None:
    f = parse_frame("HTTP/1.1 200 OK\nTransaction-ID: 1\n<status type=\"phone\"></status>")
    assert f.body == '<status type="phone"></status>'


def test_ws_url_ipv6_and_hostname() -> None:
    assert ws_url("fd7a::1", 9002) == "wss://[fd7a::1]:9002/counterpath/socketapi/v1/"
    assert ws_url("desk.example.ts.net", 9002, secure=False).startswith("ws://desk.example")


# -- against the fake server ---------------------------------------------------


async def test_status_roundtrip_and_tid(client, fake_bria: FakeBria) -> None:
    c, _ = client
    resp = await c.status("phone")
    assert resp.code == 200
    assert resp.root is not None and resp.root.findtext("state") == "ready"
    assert fake_bria.requests[-1].headers["transaction-id"]


async def test_concurrent_requests_are_correlated(client, fake_bria: FakeBria) -> None:
    c, _ = client
    phone, presence = await asyncio.gather(c.status("phone"), c.status("presence"))
    assert phone.root.get("type") == "phone"
    assert presence.root.get("type") == "presence"


async def test_204_is_empty_not_error(client, fake_bria: FakeBria) -> None:
    c, _ = client
    fake_bria.status_responses["contact"] = Reply(204)
    resp = await c.status("contact", xml_el("email", "x@example.com"))
    assert resp.code == 204 and resp.empty


@pytest.mark.parametrize(("code", "exc"), [(400, BriaBadRequest), (503, BriaNotReady)])
async def test_error_codes_raise(client, fake_bria: FakeBria, code: int, exc: type) -> None:
    c, _ = client
    fake_bria.action_responses["call"] = Reply(code)
    with pytest.raises(exc):
        await c.request("call", xml_el("dial", None, xml_el("number", "1")))


async def test_timeout(client, fake_bria: FakeBria) -> None:
    c, _ = client
    fake_bria.no_reply.add("phone")
    with pytest.raises(BriaTimeout):
        await c.status("phone", timeout=0.2)


async def test_events_reach_callback(client, fake_bria: FakeBria) -> None:
    c, events = client
    await fake_bria.push_event("voiceMail")
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events == ["voiceMail"]


async def test_disconnect_fails_pending_and_later_requests(client, fake_bria: FakeBria) -> None:
    c, _ = client
    fake_bria.no_reply.add("phone")
    pending = asyncio.create_task(c.status("phone", timeout=5))
    await asyncio.sleep(0.05)
    await fake_bria.drop_clients()
    with pytest.raises(BriaNotConnected):
        await pending
    for _ in range(50):
        if not c.connected:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(BriaNotConnected):
        await c.status("phone")


async def test_bad_frame_does_not_drop_connection(client, fake_bria: FakeBria) -> None:
    c, _ = client
    for ws in fake_bria._sockets:
        await ws.send_str("HTTP/1.1 200 OK\r\nTransaction-ID: nope\r\n\r\n<<<not xml")
        await ws.send_str("garbage")
    assert (await c.status("phone")).code == 200


async def test_connect_refused(socket_enabled: None) -> None:
    async with aiohttp.ClientSession() as session:
        c = BriaClient(session)
        with pytest.raises(BriaConnectionError):
            await c.connect("127.0.0.1", 1, ssl_context=None, timeout=2)
