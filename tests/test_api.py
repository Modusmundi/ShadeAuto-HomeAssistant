"""The thin HTTP client in api.py, exercised against the fake hub."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.shadeauto.api import (
    ShadeAutoApi,
    ShadeAutoCommandError,
    ShadeAutoConnectionError,
    clamp_position,
)

from .conftest import BOTTOM, HOST, KITCHEN, MIDDLE, MOTOR_STOP, OFFICE, FakeHub


def _api(hass: HomeAssistant) -> ShadeAutoApi:
    return ShadeAutoApi(HOST, async_get_clientsession(hass), send_spacing_sec=0.0)


def test_clamp_position() -> None:
    assert clamp_position(-5) == 0
    assert clamp_position(250) == 100
    assert clamp_position(42.9) == 42


async def test_transport_errors_become_connection_errors(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.offline = True
    api = _api(hass)
    with pytest.raises(ShadeAutoConnectionError):
        await api.status()
    with pytest.raises(ShadeAutoConnectionError):
        await api.control(KITCHEN, bottom=10)


async def test_registration_learns_the_thing_name(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    assert api.thing_name is None
    await api.registration()
    assert api.thing_name == hub.thing_name


async def test_get_all_peripheral_needs_registration_first(hass: HomeAssistant, hub: FakeHub) -> None:
    """Without the hub's ThingName the hub answers with an error document and no peripherals."""
    api = _api(hass)
    assert await api.get_all_peripheral() == []

    await api.registration()
    peripherals = await api.get_all_peripheral()
    assert {int(p["PeripheralUID"]) for p in peripherals} == set(hub.shades)
    assert {p["PeripheralName"] for p in peripherals} >= {"Kitchen", "Master Bedroom", "PerfectSheer_38A4"}


async def test_status_returns_one_record_per_shade(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    items = await api.status()
    assert {item["PeripheralUID"] for item in items} == set(hub.shades)
    kitchen = next(item for item in items if item["PeripheralUID"] == KITCHEN)
    assert kitchen[BOTTOM] == 0
    assert kitchen[MIDDLE] == 50


async def test_control_payload_shape(hass: HomeAssistant, hub: FakeHub) -> None:
    """Control carries the hub's identity, a 31-bit TaskID and strictly increasing timestamps."""
    api = _api(hass)
    await api.registration()

    reply = await api.control(KITCHEN, bottom=40)
    await api.control(str(KITCHEN), bottom=60, middle=0)

    first, second = hub.commands
    assert first["PeripheralUID"] == KITCHEN
    assert second["PeripheralUID"] == KITCHEN  # numeric strings are sent as ints
    assert first[BOTTOM] == 40
    assert MIDDLE not in first
    assert second[BOTTOM] == 60
    assert second[MIDDLE] == 0
    assert first["ThingName"] == hub.thing_name
    assert 0 < first["TaskID"] < 2**31
    assert second["Timestamp"] > first["Timestamp"]
    assert reply["Error"] == 0
    assert reply[BOTTOM] == 40
    assert hub.position(KITCHEN) == 60


async def test_control_clamps_and_sends_both_axes(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    await api.control(OFFICE, bottom=-10, middle=250)
    assert hub.commands[-1][BOTTOM] == 0
    assert hub.commands[-1][MIDDLE] == 100
    assert hub.position(OFFICE) == 0
    assert hub.position(OFFICE, MIDDLE) == 100


async def test_control_requires_bottom() -> None:
    """The hub rejects middle-only commands, so the client does not even offer that shape."""
    with pytest.raises(TypeError):
        ShadeAutoApi.control(None, KITCHEN, middle=50)  # type: ignore[call-arg,misc]


async def test_hub_error_code_raises_command_error(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.reject = True
    api = _api(hass)
    with pytest.raises(ShadeAutoCommandError) as excinfo:
        await api.control(KITCHEN, bottom=0, middle=100)
    assert excinfo.value.code == 2
    assert excinfo.value.payload[BOTTOM] == 0


async def test_stop_sends_motor_stop_only(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    await api.registration()
    reply = await api.stop(KITCHEN)
    cmd = hub.commands[-1]
    assert cmd[MOTOR_STOP] == 170
    assert BOTTOM not in cmd
    assert reply[MOTOR_STOP] == 170
