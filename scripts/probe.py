#!/usr/bin/env python3
"""Probe a Bria Desktop API endpoint and print what it actually does.

Standard library only (Python 3.10+), so it runs on macOS, Windows, Linux or the
Home Assistant SSH add-on without installing anything.

    python3 probe.py <address> check     # TLS, connection styles, every status reply
    python3 probe.py <address> watch     # print every frame; place/receive calls meanwhile
    python3 probe.py <address> missed    # does querying missedCall reset Bria's badge?

<address> is the IP or hostname of the machine running Bria. Use --port if Bria
is not on 9002. Nothing here changes Bria's settings; `missed` sends one
``status missedCall`` request, which may reset the missed-call counter (that is
what it tests).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import os
import socket
import ssl
import struct
import sys
import xml.etree.ElementTree as ET

BRIA_HOST = "cpclientapi.softphone.com"
WS_PATH = "/counterpath/socketapi/v1/"
DECL = '<?xml version="1.0" encoding="utf-8" ?>'


def ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, text frames only)
# ---------------------------------------------------------------------------


class WS:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r = reader
        self.w = writer

    @classmethod
    async def connect(
        cls,
        address: str,
        port: int,
        *,
        verify: bool,
        sni: str | None,
        host_header: str,
        timeout: float = 5,
    ) -> "WS":
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port, ssl=ctx, server_hostname=sni or ""),
            timeout,
        )
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            "User-Agent: bria-probe\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            writer.close()
            raise ConnectionError(f"handshake refused: {status}")
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.w.write(hdr + mask + masked)
        await self.w.drain()

    async def recv(self) -> str | None:
        """Return the next text message, or None when the server closes."""
        parts: list[bytes] = []
        while True:
            b1, b2 = await self.r.readexactly(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", await self.r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", await self.r.readexactly(8))
            mask = await self.r.readexactly(4) if b2 & 0x80 else None
            payload = await self.r.readexactly(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:  # ping -> pong
                self.w.write(struct.pack("!BB", 0x8A, 0x80 | len(payload)) + b"\0\0\0\0" + payload)
                continue
            if opcode in (0x1, 0x0, 0x2):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode(errors="replace")

    def close(self) -> None:
        self.w.close()


# ---------------------------------------------------------------------------
# Bria envelope
# ---------------------------------------------------------------------------

_tid = 0


def request(path: str, body: str = "") -> tuple[str, str]:
    global _tid
    _tid += 1
    tid = f"probe-{_tid}"
    full = f"{DECL}{body}" if body else ""
    msg = (
        f"GET /{path}\r\nUser-Agent: bria-probe\r\nTransaction-ID: {tid}\r\n"
        f"Content-Type: application/xml\r\nContent-Length: {len(full.encode())}"
    )
    if full:
        msg += f"\r\n\r\n{full}"
    return tid, msg


def status_body(stype: str, extra: str = "") -> str:
    return f"<status><type>{stype}</type>{extra}</status>"


def first_line(frame: str) -> str:
    return frame.split("\n", 1)[0].strip()


def header(frame: str, name: str) -> str | None:
    for line in frame.replace("\r\n", "\n").split("\n")[1:]:
        if not line.strip():
            break
        k, _, v = line.partition(":")
        if k.strip().lower() == name.lower():
            return v.strip()
    return None


def body_of(frame: str) -> str:
    i = frame.find("<")
    return frame[i:] if i >= 0 else ""


async def ask(ws: WS, path: str, body: str = "", wait: float = 4) -> list[str]:
    """Send one request; return every frame received within `wait` seconds."""
    tid, msg = request(path, body)
    await ws.send(msg)
    frames: list[str] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + wait
    while (left := end - loop.time()) > 0:
        try:
            f = await asyncio.wait_for(ws.recv(), left)
        except asyncio.TimeoutError:
            break
        if f is None:
            frames.append("<<server closed connection>>")
            break
        frames.append(f)
        if header(f, "Transaction-ID") == tid:
            break
    return frames


def show(label: str, frames: list[str]) -> None:
    print(f"\n--- {label}")
    if not frames:
        print("    (no reply within timeout)")
    for f in frames:
        print("    " + f.replace("\r\n", "\n").replace("\n", "\n    "))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


async def try_connect(args: argparse.Namespace, label: str, **kw) -> WS | None:  # type: ignore[no-untyped-def]
    try:
        ws = await WS.connect(args.address, args.port, **kw)
    except ssl.SSLCertVerificationError as e:
        print(f"[FAIL] {label}: certificate verification failed: {e.verify_message} ({e})")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label}: {type(e).__name__}: {e}")
    else:
        print(f"[ OK ] {label}")
        return ws
    return None


async def cert_info(args: argparse.Namespace) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection(args.address, args.port, ssl=ctx, server_hostname=BRIA_HOST), 5
        )
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] TLS connect: {e}")
        return
    sslobj = w.get_extra_info("ssl_object")
    der = sslobj.getpeercert(binary_form=True)
    print(f"[info] TLS {sslobj.version()} cipher {sslobj.cipher()[0]}, peer cert {len(der)} bytes")
    pem = ssl.DER_cert_to_PEM_cert(der)
    try:  # decode subject/expiry without third-party libs
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
            fh.write(pem)
        info = ssl._ssl._test_decode_cert(fh.name)  # type: ignore[attr-defined]
        os.unlink(fh.name)
        print(f"[info] subject: {info.get('subject')}")
        print(f"[info] issuer:  {info.get('issuer')}")
        print(f"[info] valid:   {info.get('notBefore')} -> {info.get('notAfter')}")
    except Exception as e:  # noqa: BLE001
        print(f"[info] could not decode certificate: {e}")
    w.close()


async def mode_check(args: argparse.Namespace) -> None:
    print(f"== Probing {args.address}:{args.port}\n")
    await cert_info(args)

    print("\n== V2: certificate verification against this machine's CA store")
    ws = await try_connect(args, "verify=on, SNI+Host=cpclientapi", verify=True, sni=BRIA_HOST,
                           host_header=f"{BRIA_HOST}:{args.port}")
    if ws:
        ws.close()

    print("\n== V1: connection styles (verification off)")
    styles = [
        ("SNI+Host=cpclientapi (planned)", BRIA_HOST, f"{BRIA_HOST}:{args.port}"),
        ("SNI=cpclientapi, Host=<address>", BRIA_HOST, f"{args.address}:{args.port}"),
        ("no SNI, Host=<address>", None, f"{args.address}:{args.port}"),
    ]
    ws_main: WS | None = None
    for label, sni, host in styles:
        ws = await try_connect(args, label, verify=False, sni=sni, host_header=host)
        if ws and ws_main is None:
            ws_main = ws
        elif ws:
            ws.close()
    if ws_main is None:
        print("\nNo connection style worked; stopping.")
        return

    print("\n== Status replies (V4: a 503 or silence here may mean Allow Access is pending)")
    for stype, extra in [
        ("authentication", ""),
        ("phone", ""),
        ("systemInformation", ""),
        ("systemSettings", ""),
        ("presence", ""),
        ("supportedPresence", ""),
        ("call", ""),
        ("audioProperties", ""),
        ("audioDevices", ""),  # V6: role strings
        ("callOptions", ""),
        ("voiceMail", ""),
        ("account", "<accountType>sip</accountType>"),
        ("account", "<accountType>xmpp</accountType>"),
        ("callHistory", "<count>3</count><entryType>all</entryType>"),
        ("screenShare", ""),  # V7: documented casing
        ("screenshare", ""),  # V7: casing the old code used
    ]:
        show(f"status {stype} {extra}".strip(), await ask(ws_main, "status", status_body(stype, extra)))
    print("\n(missedCall deliberately not queried here; use the `missed` mode.)")
    ws_main.close()


async def mode_watch(args: argparse.Namespace) -> None:
    ws = await try_connect(args, "connect", verify=False, sni=BRIA_HOST,
                           host_header=f"{BRIA_HOST}:{args.port}")
    if not ws:
        return
    print("\nWatching. Every frame is printed with a timestamp. On each call event the")
    print("call status is fetched and participant states are summarised (V5).")
    print("Try: call this Bria from another phone, let it ring, answer, hang up.")
    print("Then place an outgoing call from Bria. Ctrl+C to stop.\n")
    pending: dict[str, str] = {}

    async def poll_calls() -> None:
        tid, msg = request("status", status_body("call"))
        pending[tid] = "call"
        await ws.send(msg)

    tid, msg = request("status", status_body("authentication"))
    await ws.send(msg)
    while True:
        f = await ws.recv()
        if f is None:
            print(f"{ts()} <<server closed connection>>")
            return
        print(f"{ts()} {first_line(f)}  {body_of(f)[:400]}")
        b = body_of(f)
        if first_line(f).startswith("POST") and 'type="call"' in b:
            await poll_calls()
        t = header(f, "Transaction-ID")
        if t in pending:
            pending.pop(t)
            try:
                root = ET.fromstring(b.split("?>", 1)[-1])
                for call in root.findall("call"):
                    states = [
                        f"{p.findtext('number')}={p.findtext('state')}"
                        for p in call.findall("participants/participant")
                    ]
                    print(f"{ts()}   CALL {call.findtext('id')} hold={call.findtext('holdStatus')} {states}")
                if not root.findall("call"):
                    print(f"{ts()}   CALL (none)")
            except ET.ParseError:
                pass


async def mode_missed(args: argparse.Namespace) -> None:
    print("V3: does `GET /status missedCall` reset Bria's missed-call badge?\n")
    print("1. Miss a call on this Bria (call it, let it ring out). Do NOT click the")
    print("   missed-calls icon in Bria.")
    input("2. Note the missed-call badge number in Bria, then press Enter... ")
    ws = await try_connect(args, "connect", verify=False, sni=BRIA_HOST,
                           host_header=f"{BRIA_HOST}:{args.port}")
    if not ws:
        return
    show("status missedCall (1st)", await ask(ws, "status", status_body("missedCall")))
    show("status missedCall (2nd)", await ask(ws, "status", status_body("missedCall")))
    ws.close()
    print("\n3. Look at Bria now. Did the badge go to zero? Report that, plus both counts above.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("address", help="IP or hostname of the machine running Bria")
    p.add_argument("mode", choices=["check", "watch", "missed"], nargs="?", default="check")
    p.add_argument("--port", type=int, default=9002)
    args = p.parse_args()
    fn = {"check": mode_check, "watch": mode_watch, "missed": mode_missed}[args.mode]
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        pass
    except socket.gaierror as e:
        sys.exit(f"cannot resolve {args.address}: {e}")


if __name__ == "__main__":
    main()
