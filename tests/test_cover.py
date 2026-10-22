"""Cover entity behaviour against the fake hub: lift, vane tilt, stop, errors."""

from __future__ import annotations

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_CLOSE_COVER_TILT,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_COVER_TILT,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_STOP_COVER_TILT,
    CoverState,
)
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import BOTTOM, COVER, DINING, KITCHEN, MIDDLE, MOTOR_STOP, OFFICE, FakeHub, make_entry, setup_integration

ALL_FEATURES = 255  # OPEN|CLOSE|SET_POSITION|STOP|OPEN_TILT|CLOSE_TILT|SET_TILT_POSITION|STOP_TILT
LIFT_ONLY_FEATURES = 15


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


def _last(hub: FakeHub) -> dict:
    return hub.commands[-1]


async def test_initial_state_mirrors_hub_positions_and_tilt(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    kitchen = hass.states.get(COVER[KITCHEN])
    assert kitchen.state == CoverState.CLOSED
    assert kitchen.attributes[ATTR_CURRENT_POSITION] == 0
    assert kitchen.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    assert kitchen.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    assert kitchen.attributes[ATTR_DEVICE_CLASS] == "shade"
    assert kitchen.attributes["friendly_name"] == "Kitchen"

    dining = hass.states.get(COVER[DINING])
    assert dining.state == CoverState.OPEN
    assert dining.attributes[ATTR_CURRENT_POSITION] == 100
    assert dining.attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_lift_commands_send_bottom_with_explicit_vane_target(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Every lift command carries MiddleRailPosition so the hub cannot silently close the vanes."""
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[OFFICE], **{ATTR_POSITION: 40})
    cmd = _last(hub)
    assert cmd["PeripheralUID"] == OFFICE
    assert cmd[BOTTOM] == 40
    assert cmd[MIDDLE] == 0  # vanes cannot be open away from the bottom
    assert cmd["ThingName"] == hub.thing_name
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40

    await _call(hass, SERVICE_OPEN_COVER, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 100
    assert _last(hub)[MIDDLE] == 0
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 100

    await _call(hass, SERVICE_CLOSE_COVER, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 0  # the vanes were closed, so closing keeps them closed
    assert hass.states.get(COVER[OFFICE]).state == CoverState.CLOSED


async def test_closing_a_shade_keeps_its_vane_setting(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Kitchen is down with vanes half open; close_cover and set_position 0 must not slam them shut."""
    await _call(hass, SERVICE_CLOSE_COVER, COVER[KITCHEN])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 50

    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 0})
    assert _last(hub)[MIDDLE] == 50
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 50


async def test_tilt_commands_lower_the_shade_and_set_the_vanes(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Vanes only work at the bottom, so a tilt request is sent as (bottom 0, middle tilt)."""
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, COVER[OFFICE], **{ATTR_TILT_POSITION: 75})
    cmd = _last(hub)
    assert cmd[BOTTOM] == 0
    assert cmd[MIDDLE] == 75
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 75

    await _call(hass, SERVICE_OPEN_COVER_TILT, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 100
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_TILT_POSITION] == 100

    await _call(hass, SERVICE_CLOSE_COVER_TILT, COVER[OFFICE])
    assert _last(hub)[MIDDLE] == 0
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_raising_a_shade_closes_its_vanes_as_the_hub_does(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 50})
    state = hass.states.get(COVER[KITCHEN])
    assert state.attributes[ATTR_CURRENT_POSITION] == 50
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_stop_sends_motor_stop_and_settles_where_the_hub_says(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    hub.stuck = True  # the shade never reaches its target on its own
    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _call(hass, SERVICE_STOP_COVER, COVER[KITCHEN])
    assert _last(hub)[MOTOR_STOP] == 170
    assert BOTTOM not in _last(hub)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED  # it never moved; the hub re-targeted it to 0
    assert state.attributes[ATTR_CURRENT_POSITION] == 0

    await _call(hass, SERVICE_STOP_COVER_TILT, COVER[KITCHEN])
    assert _last(hub)[MOTOR_STOP] == 170


async def test_hub_rejection_surfaces_as_a_service_error(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    hub.reject = True
    with pytest.raises(HomeAssistantError, match="Error 2"):
        await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    # The optimistic pending target is dropped once the hub's next poll shows no change.
    hub.reject = False
    await hass.async_block_till_done()


async def test_tilt_can_be_turned_off_in_options(hass: HomeAssistant, hub: FakeHub) -> None:
    entry = make_entry({"tilt_enabled": False})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    state = hass.states.get(COVER[KITCHEN])
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
