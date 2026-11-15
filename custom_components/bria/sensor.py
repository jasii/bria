"""Bria VoIP sensor entities."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    GROUP_CALL_DETAILS,
    GROUP_MISSED_VOICEMAIL,
    GROUP_PHONE_INFO,
    GROUP_PRESENCE,
    GROUP_SCREEN_SHARE,
)
from .coordinator import BriaCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BriaCoordinator = hass.data[DOMAIN][entry.entry_id]
    opts = entry.options

    def grp(key: str) -> bool:
        return opts.get(key, True)

    # Core sensors — always enabled
    entities: list[SensorEntity] = [
        BriaActiveCallCountSensor(coordinator, entry),
    ]

    if grp(GROUP_PRESENCE):
        entities.append(BriaPresenceTextSensor(coordinator, entry))

    if grp(GROUP_CALL_DETAILS):
        entities += [
            BriaCallerNumberSensor(coordinator, entry),
            BriaCallerNameSensor(coordinator, entry),
            BriaCallHistorySensor(coordinator, entry),
        ]

    if grp(GROUP_SCREEN_SHARE):
        entities.append(BriaScreenShareStatusSensor(coordinator, entry))

    if grp(GROUP_PHONE_INFO):
        entities += [
            BriaPhoneAccountStatusSensor(coordinator, entry),
            BriaVersionSensor(coordinator, entry),
            BriaAccountsSensor(coordinator, entry),
        ]

    if grp(GROUP_MISSED_VOICEMAIL):
        entities += [
            BriaMissedCallCountSensor(coordinator, entry),
            BriaVoicemailCountSensor(coordinator, entry),
        ]

    async_add_entities(entities)


class _BriaBaseSensor(CoordinatorEntity[BriaCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BriaCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator._host)},
            "name": "Bria VoIP",
            "manufacturer": "CounterPath",
            "model": "Bria Desktop",
        }


class BriaPresenceTextSensor(_BriaBaseSensor):
    _attr_translation_key = "presence_text"
    _attr_icon = "mdi:message-text"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "presence_text")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("presence_text")


class BriaActiveCallCountSensor(_BriaBaseSensor):
    _attr_translation_key = "active_call_count"
    _attr_icon = "mdi:phone-in-talk"
    _attr_native_unit_of_measurement = "calls"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "active_call_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("active_calls", []))


class BriaCallerNumberSensor(_BriaBaseSensor):
    _attr_translation_key = "caller_number"
    _attr_icon = "mdi:phone"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "caller_number")

    @property
    def native_value(self) -> str | None:
        calls = self.coordinator.data.get("active_calls", [])
        if calls and calls[0]["participants"]:
            return calls[0]["participants"][0]["number"] or None
        return None


class BriaCallerNameSensor(_BriaBaseSensor):
    _attr_translation_key = "caller_name"
    _attr_icon = "mdi:account"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "caller_name")

    @property
    def native_value(self) -> str | None:
        calls = self.coordinator.data.get("active_calls", [])
        if calls and calls[0]["participants"]:
            return calls[0]["participants"][0]["display_name"] or None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        calls = self.coordinator.data.get("active_calls", [])
        if not calls:
            return {}
        call = calls[0]
        first_p = call["participants"][0] if call["participants"] else {}
        return {
            "call_id": call["id"],
            "call_state": first_p.get("state"),
            "call_start_time": first_p.get("start_time"),
            "on_hold": call["hold"],
            "recording": call["recording"],
        }


class BriaScreenShareStatusSensor(_BriaBaseSensor):
    _attr_translation_key = "screen_share_status"
    _attr_icon = "mdi:monitor-share"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "screen_share_status")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("screen_share_status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        url = self.coordinator.data.get("screen_share_url")
        return {"join_url": url} if url else {}


class BriaPhoneAccountStatusSensor(_BriaBaseSensor):
    _attr_translation_key = "phone_account_status"
    _attr_icon = "mdi:phone-settings"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "phone_account_status")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("phone_account_status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "phone_state": self.coordinator.data.get("phone_state"),
            "max_lines": self.coordinator.data.get("phone_max_lines"),
            "account_failure_code": self.coordinator.data.get("phone_account_failure_code"),
        }


class BriaVersionSensor(_BriaBaseSensor):
    _attr_translation_key = "bria_version"
    _attr_icon = "mdi:information"

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "bria_version")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("system_product_version")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        build = self.coordinator.data.get("system_product_build")
        if build:
            attrs["build"] = build
        call_type = self.coordinator.data.get("system_default_call_type")
        if call_type:
            attrs["default_call_type"] = call_type
        call_right_away = self.coordinator.data.get("system_call_right_away")
        if call_right_away is not None:
            attrs["call_right_away"] = call_right_away
        return attrs


class BriaMissedCallCountSensor(_BriaBaseSensor):
    _attr_translation_key = "missed_call_count"
    _attr_icon = "mdi:phone-missed"
    _attr_native_unit_of_measurement = "calls"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "missed_call_count")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("missed_call_count")


class BriaVoicemailCountSensor(_BriaBaseSensor):
    _attr_translation_key = "voicemail_count"
    _attr_icon = "mdi:voicemail"
    _attr_native_unit_of_measurement = "messages"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "voicemail_count")

    @property
    def native_value(self) -> int | None:
        accounts = self.coordinator.data.get("voicemail_accounts", [])
        if not accounts:
            return None
        totals = [a["count"] for a in accounts if a["count"] is not None and a["count"] >= 0]
        return sum(totals) if totals else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"accounts": self.coordinator.data.get("voicemail_accounts", [])}


class BriaAccountsSensor(_BriaBaseSensor):
    _attr_translation_key = "account_count"
    _attr_icon = "mdi:account-multiple"
    _attr_native_unit_of_measurement = "accounts"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "account_count")

    @property
    def native_value(self) -> int:
        sip = self.coordinator.data.get("sip_accounts", [])
        xmpp = self.coordinator.data.get("xmpp_accounts", [])
        return sum(1 for a in (sip + xmpp) if a.get("registered"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "sip_accounts": self.coordinator.data.get("sip_accounts", []),
            "xmpp_accounts": self.coordinator.data.get("xmpp_accounts", []),
        }


class BriaCallHistorySensor(_BriaBaseSensor):
    _attr_translation_key = "call_history"
    _attr_icon = "mdi:history"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BriaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "call_history")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("call_history", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entries = self.coordinator.data.get("call_history", [])

        # Build grouped-by-number list sorted by most-recent call, newest first.
        # Each group keeps the last 5 call types for direction-icon display.
        groups: dict[str, dict[str, Any]] = {}
        for e in entries:
            num = e["number"] or "unknown"
            if num not in groups:
                groups[num] = {
                    "display_name": e["display_name"] or num,
                    "number": num,
                    "count": 0,
                    "last_time_ts": 0,
                    "last_type": e["type"],
                    "recent_types": [],
                }
            g = groups[num]
            g["count"] += 1
            try:
                ts = int(e.get("time_initiated") or 0)
            except (ValueError, TypeError):
                ts = 0
            if ts > g["last_time_ts"]:
                g["last_time_ts"] = ts
                g["last_type"] = e["type"]
                if e["display_name"]:
                    g["display_name"] = e["display_name"]
            if len(g["recent_types"]) < 5:
                g["recent_types"].append(e["type"])

        grouped = sorted(
            groups.values(),
            key=lambda x: x["last_time_ts"],
            reverse=True,
        )
        return {"grouped": grouped, "entries": entries}
