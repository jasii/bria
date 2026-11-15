"""Several computers in one entry: flows, migration, live merge against two fake servers."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bria.config_flow import AddressError, normalise_address, parse_addresses
from custom_components.bria.const import DOMAIN

from .conftest import machine, v3_entry, wait_ready
from .fake_bria import FakeBria, Reply, default_status_responses
from .test_init import _until

ON_CALL = "binary_sensor.bria_voip_on_call"


@pytest.fixture
async def laptop(socket_enabled: None) -> AsyncGenerator[FakeBria]:
    fake = FakeBria(status_responses=default_status_responses())
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def two_machines(hass: HomeAssistant, fake_bria: FakeBria, laptop: FakeBria, point_at_fake: None):  # type: ignore[no-untyped-def]
    entry = v3_entry(
        9002,
        machine("Desktop", f"127.0.0.1:{fake_bria.port}"),
        machine("Laptop", f"127.0.0.1:{laptop.port}"),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_ready(entry)
    await hass.async_block_till_done()
    return entry


def ringing_call(cid: str) -> Reply:
    return Reply(
        body=(
            f'<status type="call"><call><id>{cid}</id><holdStatus>offHold</holdStatus>'
            "<participants><participant><number>555</number><displayName>Caller</displayName>"
            "<state>ringing</state><timeInitiated>0</timeInitiated></participant></participants>"
            "</call></status>"
        )
    )


# -- address parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 192.168.1.50 ", "192.168.1.50"),
        ("wss://Desk.Tail1234.TS.net:9002/counterpath", "desk.tail1234.ts.net:9002"),
        ("[fd7a::1]:9003", "[fd7a::1]:9003"),
        ("fd7a::1", "fd7a::1"),
    ],
)
def test_normalise_address(raw: str, expected: str) -> None:
    assert normalise_address(raw) == expected


@pytest.mark.parametrize("raw", ["bad host", "1.2.3.4:99999", "-x.com", "[::1]x"])
def test_normalise_address_rejects(raw: str) -> None:
    with pytest.raises(AddressError):
        normalise_address(raw)


def test_parse_addresses_dedup_and_limits() -> None:
    assert parse_addresses("a.lan\n100.64.0.1, a.lan") == ["a.lan", "100.64.0.1"]
    with pytest.raises(AddressError, match="no_addresses"):
        parse_addresses("  ")
    with pytest.raises(AddressError, match="too_many_addresses"):
        parse_addresses("a b c d e")


# -- live merge -----------------------------------------------------------------


async def test_both_connected_merge_and_answer_routing(
    hass: HomeAssistant, fake_bria: FakeBria, laptop: FakeBria, two_machines
) -> None:
    coordinator = two_machines.runtime_data
    assert {m.name for m in coordinator.connected_machines} == {"Desktop", "Laptop"}
    assert set(coordinator.data["machines"]) == {"desktop", "laptop"}

    laptop.status_responses["call"] = ringing_call("7")
    await laptop.push_event("call")
    await _until(hass, lambda: hass.states.get(ON_CALL).state == "on")
    assert coordinator.data["active_machine"] == "laptop"
    attrs = hass.states.get("sensor.bria_voip_caller_name").attributes
    assert attrs["call_id"] == "7"

    await hass.services.async_call(DOMAIN, "answer_call", {}, blocking=True)
    assert laptop.requests_for("answer")
    assert not fake_bria.requests_for("answer")


async def test_presence_goes_to_every_machine(
    hass: HomeAssistant, fake_bria: FakeBria, laptop: FakeBria, two_machines
) -> None:
    await hass.services.async_call(DOMAIN, "set_presence", {"status": "busy"}, blocking=True)
    for fake in (fake_bria, laptop):
        assert fake.requests_for("setPresence")[-1].root.findtext("presenceStatus") == "busy"


async def test_one_machine_drops_other_stays(
    hass: HomeAssistant, fake_bria: FakeBria, laptop: FakeBria, two_machines
) -> None:
    coordinator = two_machines.runtime_data
    await laptop.stop()
    await _until(hass, lambda: not coordinator.data["machines"]["laptop"]["connected"])
    assert coordinator.data["connected"] is True
    assert hass.states.get("binary_sensor.bria_voip_connected").state == "on"
    assert coordinator.data["active_machine"] == "desktop"


# -- migration and flows ----------------------------------------------------------


async def test_migrate_v2_to_v3(hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="Bria VoIP", data={"host": "127.0.0.1", "port": fake_bria.port}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_ready(entry)
    assert entry.version == 3
    assert entry.data == {
        "port": fake_bria.port,
        "verify_ssl": True,
        "machines": [{"id": "main", "name": "Bria", "addresses": ["127.0.0.1"]}],
    }
    assert hass.states.get("binary_sensor.bria_voip_connected").state == "on"


async def test_user_flow_tests_connection(
    hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Desktop", "addresses": "127.0.0.1:1", "port": 9002}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert "no answer" in result["description_placeholders"]["results"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Desktop", "addresses": f"127.0.0.1:1\n127.0.0.1:{fake_bria.port}", "port": 9002},
    )
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["machines"][0]["addresses"] == ["127.0.0.1:1", f"127.0.0.1:{fake_bria.port}"]
    await hass.async_block_till_done()


async def test_user_flow_save_anyway_and_not_authorized(
    hass: HomeAssistant, fake_bria: FakeBria, point_at_fake: None
) -> None:
    fake_bria.no_reply.add("systemInformation")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bria.machine.TEST_TIMEOUT", 0.3)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Laptop", "addresses": f"127.0.0.1:{fake_bria.port}", "port": 9002}
        )
    assert result["errors"] == {"base": "not_authorized"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Laptop", "addresses": "127.0.0.1:1", "port": 9002, "save_anyway": True},
    )
    assert result["step_id"] == "sensors"


async def test_options_add_edit_remove_machine(
    hass: HomeAssistant, fake_bria: FakeBria, laptop: FakeBria, setup_bria
) -> None:
    entry = setup_bria
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_machine"})
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "bria", "addresses": f"127.0.0.1:{laptop.port}"}
    )
    assert flow["errors"] == {"name": "name_in_use"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "Laptop", "addresses": f"127.0.0.1:{laptop.port}"}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await wait_ready(entry)
    assert [m["name"] for m in entry.data["machines"]] == ["Bria", "Laptop"]
    assert len(entry.runtime_data.connected_machines) == 2

    laptop_id = entry.data["machines"][1]["id"]
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "edit_machine"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"machine": laptop_id})
    assert flow["step_id"] == "edit_machine_details"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "Work laptop", "addresses": f"127.0.0.1:{laptop.port}"}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["machines"][1] == {"id": laptop_id, "name": "Work laptop", "addresses": [f"127.0.0.1:{laptop.port}"]}

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "remove_machine"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"machine": laptop_id})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [m["name"] for m in entry.data["machines"]] == ["Bria"]

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "remove_machine"})
    assert flow["reason"] == "last_machine"


async def test_reconfigure(hass: HomeAssistant, fake_bria: FakeBria, setup_bria) -> None:
    entry = setup_bria
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"port": fake_bria.port, "verify_ssl": False}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["verify_ssl"] is False
    await hass.async_block_till_done()
    await wait_ready(entry)
