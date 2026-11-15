"""Merge rules for several machines (PLAN 3c)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from custom_components.bria.machine import _default_data
from custom_components.bria.merge import _via, merge


@dataclass
class M:
    id: str
    name: str
    data: dict[str, Any] = field(default_factory=_default_data)
    address: str | None = None
    last_activity: float = 0.0

    @property
    def connected(self) -> bool:
        return bool(self.data.get("connected"))


def up(m: M, **values: Any) -> M:
    m.data.update(connected=True, **values)
    m.address = m.address or "192.0.2.1"
    return m


def call(cid: str, state: str, start: int = 0) -> dict[str, Any]:
    return {
        "id": cid,
        "hold": False,
        "hold_status": "offHold",
        "recording": "",
        "recording_file": "",
        "participants": [
            {
                "number": "100",
                "display_name": "X",
                "state": state,
                "start_time": datetime.fromtimestamp(start, UTC) if start else None,
            }
        ],
    }


def test_nothing_connected() -> None:
    out = merge([M("a", "Desktop"), M("b", "Laptop")])
    assert out["connected"] is False
    assert out["active_machine"] is None
    assert out["machines"]["a"]["connected"] is False


def test_any_connected_and_first_in_order_is_active() -> None:
    a, b = M("a", "Desktop"), up(M("b", "Laptop"), presence_status="away")
    out = merge([a, b])
    assert out["connected"] is True
    assert out["active_machine"] == "b"
    assert out["presence_status"] == "away"
    a = up(a, presence_status="available")
    assert merge([a, b])["active_machine"] == "a"


def test_last_used_wins_without_calls() -> None:
    a = up(M("a", "Desktop"), presence_status="available")
    b = up(M("b", "Laptop"), presence_status="busy")
    b.last_activity = 10.0
    assert merge([a, b])["presence_status"] == "busy"


def test_calls_union_tagged_ringing_first_and_owner_active() -> None:
    a = up(M("a", "Desktop"), active_calls=[call("1", "connected", 100)])
    b = up(M("b", "Laptop"), active_calls=[call("1", "ringing")])
    a.last_activity = 50.0
    out = merge([a, b])
    assert [(c["machine"], c["id"]) for c in out["active_calls"]] == [("b", "1"), ("a", "1")]
    assert out["active_calls"][0]["machine_name"] == "Laptop"
    assert out["active_machine"] == "b"


def test_disconnected_machine_calls_ignored() -> None:
    a = M("a", "Desktop")
    a.data["active_calls"] = [call("1", "connected")]
    b = up(M("b", "Laptop"))
    assert merge([a, b])["active_calls"] == []


def test_voicemail_and_missed_not_double_counted() -> None:
    vm = lambda n: [{"account_id": "1", "account_name": "Work", "count": n}]  # noqa: E731
    a = up(M("a", "Desktop"), voicemail_accounts=vm(2), missed_call_count=3)
    b = up(M("b", "Laptop"), voicemail_accounts=vm(3), missed_call_count=None)
    out = merge([a, b])
    assert out["voicemail_accounts"] == vm(3)
    assert out["missed_call_count"] == 3


def test_accounts_deduplicated_registered_if_any() -> None:
    acc = lambda reg: [  # noqa: E731
        {"account_name": "Work", "account_id": "1", "type": "sip", "enabled": True, "registered": reg}
    ]
    out = merge([up(M("a", "D"), sip_accounts=acc(False)), up(M("b", "L"), sip_accounts=acc(True))])
    assert len(out["sip_accounts"]) == 1 and out["sip_accounts"][0]["registered"] is True


def test_via() -> None:
    assert _via("100.64.1.2") == "tailscale"
    assert _via("100.200.1.2") == "lan"
    assert _via("desk.tail1234.ts.net") == "tailscale"
    assert _via("192.168.1.50") == "lan"
    assert _via(None) is None
