"""Entity base, device identity, groups and migration."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bria.const import (
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    GROUP_DEFAULTS,
    GROUP_SCREEN_SHARE,
    SENSOR_GROUPS,
)

from custom_components.bria.entity import find_device

from .fake_bria import FakeBria, Reply
from .test_init import CONNECTED, _until

ON_CALL = "binary_sensor.bria_voip_on_call"


async def test_device_keyed_by_entry(hass: HomeAssistant, setup_bria) -> None:
    device = find_device(hass, setup_bria.entry_id, setup_bria.entry_id)
    assert device is not None
    assert device.manufacturer == "CounterPath"
    assert device.sw_version == "6.5.1"
    reg = er.async_get(hass)
    assert reg.async_get(CONNECTED).entity_category is EntityCategory.DIAGNOSTIC
    assert reg.async_get("switch.bria_voip_auto_answer").entity_category is EntityCategory.CONFIG


async def test_unavailable_while_disconnected(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    fake_bria.connected.clear()
    await fake_bria.stop()
    await _until(hass, lambda: hass.states.get(ON_CALL).state == STATE_UNAVAILABLE)
    # The connectivity sensor itself stays available and reports off.
    assert hass.states.get(CONNECTED).state == "off"
    assert hass.states.get(ON_CALL).attributes.get("device_class") is None


async def test_screen_share_unavailable_when_unsupported(
    hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None
) -> None:
    fake_bria.status_responses["screenShare"] = Reply(400)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Bria VoIP",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_bria.port},
        options={g: True for g in SENSOR_GROUPS},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _until(hass, lambda: entry.runtime_data.data["screen_share_supported"] is False)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bria_voip_screen_share_status").state == STATE_UNAVAILABLE


def test_screen_share_off_by_default() -> None:
    assert GROUP_DEFAULTS[GROUP_SCREEN_SHARE] is False
    assert all(v for k, v in GROUP_DEFAULTS.items() if k != GROUP_SCREEN_SHARE)


async def test_disabled_group_removes_registry_entries(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    reg = er.async_get(hass)
    assert reg.async_get("switch.bria_voip_auto_answer") is not None
    hass.config_entries.async_update_entry(
        setup_bria, options={**setup_bria.options, "group_call_options": False}
    )
    await _until(hass, lambda: reg.async_get("switch.bria_voip_auto_answer") is None, timeout=5)
    await _until(hass, lambda: setup_bria.state is ConfigEntryState.LOADED)
    assert reg.async_get("switch.bria_voip_microphone_muted") is not None


async def test_presence_lists_reported_only_states(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    fake_bria.status_responses["presence"] = Reply(
        body='<status type="presence"><presenceStatus>onThePhone</presenceStatus>'
        "<presenceText>On the phone</presenceText></status>"
    )
    await fake_bria.push_event("presence")
    await _until(hass, lambda: hass.states.get("select.bria_voip_presence_status").state == "onThePhone")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.bria_voip_presence_status", "option": "onThePhone"},
            blocking=True,
        )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.bria_voip_presence_status", "option": "away"},
        blocking=True,
    )
    assert fake_bria.requests_for("setPresence")[-1].root.findtext("presenceStatus") == "away"


async def test_migrate_v1_keeps_device_and_entities(
    hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="Bria VoIP",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_bria.port},
        options={g: True for g in SENSOR_GROUPS},
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    old = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "127.0.0.1")}, name="Bria VoIP"
    )
    ent_reg = er.async_get(hass)
    ent = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_connected",
        config_entry=entry,
        device_id=old.id,
        suggested_object_id="bria_voip_connected",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=5)
    await hass.async_block_till_done()

    assert entry.version == 3
    assert entry.data["machines"][0]["addresses"] == ["127.0.0.1"]
    moved = find_device(hass, entry.entry_id, entry.entry_id)
    assert moved is not None and moved.id == old.id
    assert find_device(hass, entry.entry_id, "127.0.0.1") is None
    assert ent_reg.async_get(ent.entity_id).device_id == old.id
    assert len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)) == 1
