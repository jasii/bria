"""Bria sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BriaConfigEntry
from .const import (
    GROUP_CALL_DETAILS,
    GROUP_MISSED_VOICEMAIL,
    GROUP_PHONE_INFO,
    GROUP_PRESENCE,
    GROUP_SCREEN_SHARE,
)
from .entity import BriaEntity, add_entities_for_groups, first_call, first_participant

PARALLEL_UPDATES = 0

type Data = dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class BriaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Data], Any]
    attrs_fn: Callable[[Data], dict[str, Any]] | None = None
    group: str | None = None


def _caller_attrs(d: Data) -> dict[str, Any]:
    call = first_call(d)
    if not call:
        return {}
    p = first_participant(d) or {}
    return {
        "call_id": call["id"],
        "call_state": p.get("state"),
        "call_start_time": p.get("start_time"),
        "on_hold": call["hold"],
        "recording": call["recording"],
    }


def _voicemail_total(d: Data) -> int | None:
    totals = [
        a["count"] for a in d.get("voicemail_accounts", []) if a["count"] is not None and a["count"] >= 0
    ]
    return sum(totals) if totals else None


def _version_attrs(d: Data) -> dict[str, Any]:
    attrs = {
        "build": d.get("system_product_build"),
        "default_call_type": d.get("system_default_call_type"),
        "call_right_away": d.get("system_call_right_away"),
    }
    return {k: v for k, v in attrs.items() if v not in (None, "")}


def _latest_history(d: Data) -> datetime | None:
    times = [e["time_initiated"] for e in d.get("call_history", []) if e.get("time_initiated")]
    return max(times, default=None)


def _history_attrs(d: Data) -> dict[str, Any]:
    entries = d.get("call_history", [])
    # Grouped by number, newest first; each group keeps its last 5 call types.
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        num = e["number"] or "unknown"
        g = groups.setdefault(
            num,
            {
                "display_name": e["display_name"] or num,
                "number": num,
                "count": 0,
                "last_time_ts": 0,
                "last_type": e["type"],
                "recent_types": [],
            },
        )
        g["count"] += 1
        when = e.get("time_initiated")
        ts = int(when.timestamp()) if when else 0
        if ts > g["last_time_ts"]:
            g["last_time_ts"] = ts
            g["last_type"] = e["type"]
            if e["display_name"]:
                g["display_name"] = e["display_name"]
        if len(g["recent_types"]) < 5:
            g["recent_types"].append(e["type"])
    grouped = sorted(groups.values(), key=lambda x: x["last_time_ts"], reverse=True)
    return {"count": len(entries), "grouped": grouped, "entries": entries}


DESCRIPTIONS: tuple[BriaSensorDescription, ...] = (
    BriaSensorDescription(
        key="active_call_count",
        translation_key="active_call_count",
        native_unit_of_measurement="calls",
        value_fn=lambda d: len(d.get("active_calls", [])),
    ),
    BriaSensorDescription(
        key="presence_text",
        translation_key="presence_text",
        group=GROUP_PRESENCE,
        value_fn=lambda d: d.get("presence_text"),
    ),
    BriaSensorDescription(
        key="caller_number",
        translation_key="caller_number",
        group=GROUP_CALL_DETAILS,
        value_fn=lambda d: (first_participant(d) or {}).get("number") or None,
    ),
    BriaSensorDescription(
        key="caller_name",
        translation_key="caller_name",
        group=GROUP_CALL_DETAILS,
        value_fn=lambda d: (first_participant(d) or {}).get("display_name") or None,
        attrs_fn=_caller_attrs,
    ),
    BriaSensorDescription(
        key="call_history",
        translation_key="call_history",
        device_class=SensorDeviceClass.TIMESTAMP,
        group=GROUP_CALL_DETAILS,
        value_fn=_latest_history,
        attrs_fn=_history_attrs,
    ),
    BriaSensorDescription(
        key="screen_share_status",
        translation_key="screen_share_status",
        group=GROUP_SCREEN_SHARE,
        value_fn=lambda d: d.get("screen_share_status"),
        attrs_fn=lambda d: {"join_url": url} if (url := d.get("screen_share_url")) else {},
    ),
    BriaSensorDescription(
        key="phone_account_status",
        translation_key="phone_account_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        group=GROUP_PHONE_INFO,
        value_fn=lambda d: d.get("phone_account_status"),
        attrs_fn=lambda d: {
            "phone_state": d.get("phone_state"),
            "max_lines": d.get("phone_max_lines"),
            "account_failure_code": d.get("phone_account_failure_code"),
        },
    ),
    BriaSensorDescription(
        key="bria_version",
        translation_key="bria_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        group=GROUP_PHONE_INFO,
        value_fn=lambda d: d.get("system_product_version"),
        attrs_fn=_version_attrs,
    ),
    BriaSensorDescription(
        key="account_count",
        translation_key="account_count",
        native_unit_of_measurement="accounts",
        entity_category=EntityCategory.DIAGNOSTIC,
        group=GROUP_PHONE_INFO,
        value_fn=lambda d: sum(
            1 for a in d.get("sip_accounts", []) + d.get("xmpp_accounts", []) if a.get("registered")
        ),
        attrs_fn=lambda d: {
            "sip_accounts": d.get("sip_accounts", []),
            "xmpp_accounts": d.get("xmpp_accounts", []),
        },
    ),
    BriaSensorDescription(
        key="missed_call_count",
        translation_key="missed_call_count",
        native_unit_of_measurement="calls",
        state_class=SensorStateClass.MEASUREMENT,
        group=GROUP_MISSED_VOICEMAIL,
        value_fn=lambda d: d.get("missed_call_count"),
    ),
    BriaSensorDescription(
        key="voicemail_count",
        translation_key="voicemail_count",
        native_unit_of_measurement="messages",
        state_class=SensorStateClass.MEASUREMENT,
        group=GROUP_MISSED_VOICEMAIL,
        value_fn=_voicemail_total,
        attrs_fn=lambda d: {"accounts": d.get("voicemail_accounts", [])},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_entities_for_groups(
        hass,
        entry,
        SENSOR_DOMAIN,
        DESCRIPTIONS,
        lambda desc: BriaSensor(coordinator, desc),
        async_add_entities,
    )


class BriaSensor(BriaEntity, SensorEntity):
    entity_description: BriaSensorDescription
    # Large or personal lists: keep them out of the recorder database.
    _unrecorded_attributes = frozenset(
        {"entries", "grouped", "accounts", "sip_accounts", "xmpp_accounts", "join_url"}
    )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.data)
