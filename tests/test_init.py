"""Integration tests: setup, state, events, disconnects."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .fake_bria import FakeBria, Reply

CONNECTED = "binary_sensor.bria_voip_connected"


async def _until(hass: HomeAssistant, predicate, timeout: float = 3) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()


async def test_setup_connect_unload(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    entry = setup_bria
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CONNECTED).state == "on"
    assert hass.states.get("sensor.bria_voip_bria_version").state == "6.5.1"
    assert hass.states.get("select.bria_voip_presence_status").state == "available"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_hold_status_values(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    fake_bria.status_responses["call"] = Reply(
        body=(
            '<status type="call"><call><id>c1</id><holdStatus>localHold</holdStatus>'
            "<participants><participant><number>100</number><displayName>A</displayName>"
            "<state>connected</state><timeInitiated> 1303931552</timeInitiated>"
            "</participant></participants></call></status>"
        )
    )
    await fake_bria.push_event("call")
    await _until(hass, lambda: hass.states.get("binary_sensor.bria_voip_call_on_hold").state == "on")
    attrs = hass.states.get("sensor.bria_voip_caller_name").attributes
    assert attrs["on_hold"] is True
    assert attrs["call_start_time"].year == 2011


async def test_disconnect_resets_state_and_reconnects(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    fake_bria.status_responses["call"] = Reply(
        body='<status type="call"><call><id>c1</id><holdStatus>offHold</holdStatus></call></status>'
    )
    await fake_bria.push_event("call")
    await _until(hass, lambda: hass.states.get("binary_sensor.bria_voip_on_call").state == "on")

    fake_bria.status_responses["call"] = Reply(body='<status type="call"></status>')
    await fake_bria.drop_clients()
    await _until(hass, lambda: hass.states.get("binary_sensor.bria_voip_on_call").state == "off")

    # Backoff is patched short; it must come back on its own.
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=3)
    await _until(hass, lambda: hass.states.get(CONNECTED).state == "on")


async def test_audio_devices_reply_type_is_ignored(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    """Bria 6.8 answers audioDevices with type="deviceConfiguration" (probe, 2026-11-14)."""
    fake_bria.status_responses["audioDevices"] = Reply(
        body=(
            '<status type="deviceConfiguration"><devices><device><name>Head &amp; Set</name>'
            '<id>625810325</id><type>output</type><selected role="headset, ringOn">true</selected>'
            "</device></devices></status>"
        )
    )
    await fake_bria.push_event("audioDevices")
    coordinator = setup_bria.runtime_data
    await _until(hass, lambda: coordinator.data["audio_devices"])
    assert coordinator.data["audio_devices"][0] == {
        "name": "Head & Set",
        "id": "625810325",
        "type": "output",
        "selected": True,
        "roles": ["headset", "ringOn"],
    }


async def test_screenshare_400_marks_unsupported(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    coordinator = setup_bria.runtime_data
    # Default fake reply for screenShare is an empty 200; make it 400 and reconnect.
    fake_bria.status_responses["screenShare"] = Reply(400)
    await fake_bria.drop_clients()
    await asyncio.wait_for(fake_bria.connected.wait(), timeout=3)
    await _until(hass, lambda: coordinator.data["screen_share_supported"] is False)
    assert "screenShare" in fake_bria.status_types_requested()


async def test_accounts_removed_when_list_empty(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    coordinator = setup_bria.runtime_data
    fake_bria.status_responses["account"] = Reply(
        body=(
            '<status type="account"><account><accountName>A</accountName><accountId>1</accountId>'
            "<type>sip</type><enabled>true</enabled><registered>true</registered></account></status>"
        )
    )
    await fake_bria.push_event("account")
    await _until(hass, lambda: coordinator.data["sip_accounts"])
    fake_bria.status_responses["account"] = Reply(body='<status type="account"></status>')
    await fake_bria.push_event("account")
    await _until(hass, lambda: coordinator.data["sip_accounts"] == [])


async def test_service_xml_is_escaped(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    await hass.services.async_call(
        "bria", "place_call", {"number": "1<2>&3"}, blocking=True
    )
    req = fake_bria.requests_for("call")[-1]
    assert req.root.findtext("number") == "1<2>&3"


async def test_service_error_surfaces(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    fake_bria.action_responses["call"] = Reply(503)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("bria", "place_call", {"number": "1"}, blocking=True)
