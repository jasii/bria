"""Safety options and admin-only services."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.bria.const import (
    CONF_ALLOW_APP_CONTROL,
    CONF_ALLOW_OUTGOING_CALLS,
    CONF_ALLOW_RECORDING,
    CONF_DIAL_ALLOW_PATTERN,
)

from .fake_bria import FakeBria


async def _set_options(hass: HomeAssistant, fake: FakeBria, entry, **opts) -> None:  # type: ignore[no-untyped-def]
    """Change options and wait for the reload to reconnect."""
    fake.connected.clear()
    hass.config_entries.async_update_entry(entry, options={**entry.options, **opts})
    await hass.async_block_till_done()
    await asyncio.wait_for(fake.connected.wait(), timeout=5)
    async with asyncio.timeout(5):
        while not entry.runtime_data.data.get("system_product_version"):
            await asyncio.sleep(0.02)
    await hass.async_block_till_done()


async def test_defaults_allow_calls_block_recording_and_app_control(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    await hass.services.async_call("bria", "place_call", {"number": "100"}, blocking=True)
    assert fake_bria.requests_for("call")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "bria", "start_call_recording", {"filename": "x", "call_id": "1"}, blocking=True
        )
    for svc in ("exit", "sign_out"):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("bria", svc, {}, blocking=True)
    assert not fake_bria.requests_for("exit")


async def test_outgoing_calls_off(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    await _set_options(hass, fake_bria, setup_bria, **{CONF_ALLOW_OUTGOING_CALLS: False})
    with pytest.raises(ServiceValidationError, match="outgoing_calls_disabled|turned off"):
        await hass.services.async_call("bria", "place_call", {"number": "100"}, blocking=True)
    assert not fake_bria.requests_for("call")


async def test_dial_pattern(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    await _set_options(hass, fake_bria, setup_bria, **{CONF_DIAL_ALLOW_PATTERN: r"1\d{2}"})
    await hass.services.async_call("bria", "place_call", {"number": "123"}, blocking=True)
    for bad in ("1234", "900", "12a", "1" * 100):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("bria", "place_call", {"number": bad}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "bria", "transfer_call", {"target_number": "900", "call_id": "c1"}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "bria", "start_attended_transfer", {"target_number": "900", "call_id": "c1"}, blocking=True
        )
    assert len(fake_bria.requests_for("call")) == 1


async def test_recording_allowed_but_filename_checked(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria
) -> None:
    await _set_options(hass, fake_bria, setup_bria, **{CONF_ALLOW_RECORDING: True})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "bria", "start_call_recording", {"filename": "../evil", "call_id": "1"}, blocking=True
        )
    await hass.services.async_call(
        "bria", "start_call_recording", {"filename": "meeting", "call_id": "1"}, blocking=True
    )


async def test_app_control_is_admin_only(
    hass: HomeAssistant, fake_bria: FakeBria, setup_bria, hass_read_only_user
) -> None:
    from homeassistant.core import Context

    await _set_options(hass, fake_bria, setup_bria, **{CONF_ALLOW_APP_CONTROL: True})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "bria", "exit", {}, blocking=True, context=Context(user_id=hass_read_only_user.id)
        )
    await hass.services.async_call("bria", "exit", {}, blocking=True)
    assert fake_bria.requests_for("exit")


async def test_options_flow_safety_step(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    entry = setup_bria
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], dict(entry.options))
    assert result["step_id"] == "safety"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DIAL_ALLOW_PATTERN: "(unclosed"}
    )
    assert result["errors"] == {CONF_DIAL_ALLOW_PATTERN: "invalid_pattern"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ALLOW_OUTGOING_CALLS: True,
            CONF_DIAL_ALLOW_PATTERN: r" \+?1\d{10} ",
            CONF_ALLOW_RECORDING: False,
            CONF_ALLOW_APP_CONTROL: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DIAL_ALLOW_PATTERN] == r"\+?1\d{10}"
    assert entry.options["group_audio"] is True
