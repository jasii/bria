"""Merge the state of several Bria machines into one combined view.

Pure functions over plain dicts; the coordinator calls ``merge`` after every
machine update. Rules (PLAN 3c):

- Connected: any machine connected.
- Calls: union over machines, each tagged with its machine; the "focus call"
  (ringing, then connected, then newest) is first in the list.
- Active machine: owner of the focus call, else the most recently used
  machine, else the first connected one in configured order. Everything that
  belongs to one Bria (presence, audio, call options, history, version) is
  read from it.
- Voicemail and missed calls: highest count per account, because the same SIP
  account on two computers reports the same messages twice.
- Accounts: union, de-duplicated by name and type.
"""
from __future__ import annotations

from typing import Any, Protocol

from .machine import _default_data

_STATE_RANK = {"ringing": 0, "connected": 1, "connecting": 2}


class MachineLike(Protocol):
    id: str
    name: str
    address: str | None
    last_activity: float
    data: dict[str, Any]

    @property
    def connected(self) -> bool: ...


def _call_rank(call: dict[str, Any]) -> tuple[int, float]:
    states = [p.get("state", "") for p in call.get("participants", [])]
    rank = min((_STATE_RANK.get(s, 3) for s in states), default=3)
    starts = [p["start_time"] for p in call.get("participants", []) if p.get("start_time")]
    newest = max(starts, default=None)
    return rank, -(newest.timestamp() if newest else 0.0)


def merged_calls(machines: list[MachineLike]) -> list[dict[str, Any]]:
    calls = [
        {**call, "machine": m.id, "machine_name": m.name}
        for m in machines
        if m.connected
        for call in m.data.get("active_calls", [])
    ]
    return sorted(calls, key=_call_rank)


def pick_active(machines: list[MachineLike], calls: list[dict[str, Any]]) -> MachineLike | None:
    connected = [m for m in machines if m.connected]
    if not connected:
        return None
    if calls:
        owner = calls[0]["machine"]
        return next(m for m in connected if m.id == owner)
    used = max(connected, key=lambda m: m.last_activity)
    return used if used.last_activity > 0 else connected[0]


def _max_count(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return max(known) if known else None


def merged_voicemail(machines: list[MachineLike]) -> list[dict[str, Any]]:
    by_account: dict[str, dict[str, Any]] = {}
    for m in machines:
        for vm in m.data.get("voicemail_accounts", []):
            key = vm.get("account_name") or vm.get("account_id") or ""
            if key not in by_account:
                by_account[key] = dict(vm)
            else:
                by_account[key]["count"] = _max_count([by_account[key]["count"], vm["count"]])
    return list(by_account.values())


def merged_accounts(machines: list[MachineLike], key: str) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for m in machines:
        for acc in m.data.get(key, []):
            ident = (acc.get("account_name", ""), acc.get("type", ""))
            if ident not in seen:
                seen[ident] = dict(acc)
            else:
                seen[ident]["registered"] = seen[ident]["registered"] or acc.get("registered", False)
                seen[ident]["enabled"] = seen[ident]["enabled"] or acc.get("enabled", False)
    return list(seen.values())


def _via(address: str | None) -> str | None:
    if not address:
        return None
    host = address.lower().rstrip(".")
    if host.endswith(".ts.net"):
        return "tailscale"
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 100 and 64 <= b <= 127:  # 100.64.0.0/10, Tailscale's range
            return "tailscale"
    if host.startswith("fd7a:115c:a1e0"):  # Tailscale IPv6 ULA prefix
        return "tailscale"
    return "lan"


def machine_summary(m: MachineLike) -> dict[str, Any]:
    return {
        "name": m.name,
        "connected": m.connected,
        "address": m.address,
        "via": _via(m.address),
        "version": m.data.get("system_product_version"),
        "phone_state": m.data.get("phone_state"),
    }


def merge(machines: list[MachineLike]) -> dict[str, Any]:
    calls = merged_calls(machines)
    active = pick_active(machines, calls)
    out = dict(active.data) if active is not None else _default_data()
    connected = [m for m in machines if m.connected]
    out.update(
        connected=bool(connected),
        active_calls=calls,
        voicemail_accounts=merged_voicemail(connected),
        missed_call_count=_max_count([m.data.get("missed_call_count") for m in connected]),
        sip_accounts=merged_accounts(connected, "sip_accounts"),
        xmpp_accounts=merged_accounts(connected, "xmpp_accounts"),
        active_machine=active.id if active is not None else None,
        machines={m.id: machine_summary(m) for m in machines},
    )
    return out
