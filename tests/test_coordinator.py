"""Motion tracking: hub as source of truth, adaptive polling, verify-and-retry."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    CoverState,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.shadeauto.const import (
    AXIS_BOTTOM,
    AXIS_MIDDLE,
    MOTION_FAILSAFE_SEC,
    PENDING_GRACE_SEC,
)
from custom_components.shadeauto.coordinator import ShadeAutoCoordinator

from .conftest import BOTTOM, COVER, KITCHEN, OFFICE, FakeHub, make_entry, setup_integration

IDLE = timedelta(seconds=30)
MOTION = timedelta(seconds=2)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Advance time and let the coordinator run its scheduled refresh."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _coordinator(entry: MockConfigEntry) -> ShadeAutoCoordinator:
    return entry.runtime_data.coordinator


async def test_polling_speeds_up_while_a_shade_moves_and_slows_down_after(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    assert coordinator.update_interval == IDLE
    assert not coordinator.any_moving()

    # The vendor app commands Office to 80: the hub's target changes first, the actual later.
    hub.settle_after_polls = 3
    hub.external_command(OFFICE, bottom=80)
    await _poll(hass, freezer, 31)

    assert coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == MOTION
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 35  # hub actual, no guessing for external moves

    for _ in range(3):
        await _poll(hass, freezer, 2)

    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 80


async def test_our_command_is_pending_until_the_hub_reflects_it_then_converges(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.settle_after_polls = 2
    await hass.services.async_call(
        COVER_DOMAIN, SERVICE_SET_COVER_POSITION, {ATTR_ENTITY_ID: COVER[KITCHEN], ATTR_POSITION: 40}, blocking=True
    )
    await hass.async_block_till_done()

    # The hub accepted the command (its target is 40) but the shade has not arrived yet.
    assert hub.target(KITCHEN) == 40
    assert coordinator.get_target(str(KITCHEN), AXIS_BOTTOM) == 40
    assert coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _poll(hass, freezer, 2)
    await _poll(hass, freezer, 2)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0  # raised shades have closed vanes


async def test_hub_position_wins_when_a_command_is_never_reflected(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Regression for the old drift bug: the entity must never keep showing a target the hub ignored."""
    entry = make_entry({"estimation_enabled": True, "full_travel_time_sec": 10})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    coordinator = _coordinator(entry)

    # A hub that answers OK but changes nothing, not even its own target.
    hub.ignore_commands = True
    await hass.services.async_call(
        COVER_DOMAIN, SERVICE_SET_COVER_POSITION, {ATTR_ENTITY_ID: COVER[KITCHEN], ATTR_POSITION: 60}, blocking=True
    )
    await hass.async_block_till_done()
    assert coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)  # pending grace window
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _poll(hass, freezer, PENDING_GRACE_SEC + 1)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0


async def test_external_move_is_reflected_without_a_retry(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    hub.external_move(KITCHEN, bottom=70, middle=0)
    await _poll(hass, freezer, 31)

    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 70
    assert hub.commands == []


async def test_verify_retries_exactly_once_when_the_shade_does_not_arrive(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    entry = make_entry({"verify_enabled": True, "verify_delay_sec": 5})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    hub.stuck = True  # hub accepts (target 100) but the shade never moves

    await hass.services.async_call(COVER_DOMAIN, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: COVER[KITCHEN]}, blocking=True)
    await hass.async_block_till_done()
    assert len(hub.commands) == 1

    await _poll(hass, freezer, 6)  # verify fires: not at target, hub target is still ours -> retry
    assert len(hub.commands) == 2
    assert hub.commands[1][BOTTOM] == 100

    await _poll(hass, freezer, 6)  # no second retry
    await _poll(hass, freezer, 6)
    assert len(hub.commands) == 2


async def test_verify_does_not_fight_a_command_from_someone_else(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    entry = make_entry({"verify_enabled": True, "verify_delay_sec": 5})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    hub.stuck = True

    await hass.services.async_call(COVER_DOMAIN, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: COVER[KITCHEN]}, blocking=True)
    await hass.async_block_till_done()
    # Before verify fires, the vendor app sends the shade back down.
    hub.external_command(KITCHEN, bottom=0, middle=100)

    await _poll(hass, freezer, 6)
    assert len(hub.commands) == 1  # we did not re-send our stale command


async def test_a_newer_command_cancels_the_older_verify(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    entry = make_entry({"verify_enabled": True, "verify_delay_sec": 5})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    hub.stuck = True

    await hass.services.async_call(COVER_DOMAIN, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: COVER[KITCHEN]}, blocking=True)
    await _poll(hass, freezer, 3)
    await hass.services.async_call(
        COVER_DOMAIN, SERVICE_SET_COVER_POSITION, {ATTR_ENTITY_ID: COVER[KITCHEN], ATTR_POSITION: 20}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(hub.commands) == 2

    await _poll(hass, freezer, 3)  # the first verify would have fired now; it was cancelled
    assert len(hub.commands) == 2
    await _poll(hass, freezer, 3)  # the second one fires and retries the second command
    assert len(hub.commands) == 3
    assert hub.commands[2][BOTTOM] == 20


async def test_stalled_shade_stops_counting_as_moving_after_the_failsafe(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.stuck = True
    hub.external_command(OFFICE, bottom=90)
    await _poll(hass, freezer, 31)
    assert coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)

    await _poll(hass, freezer, MOTION_FAILSAFE_SEC + 1)
    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE
    assert hass.states.get(COVER[OFFICE]).state == CoverState.OPEN  # hub actual 35, no motion flags


async def test_tilt_axis_is_tracked_independently(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.settle_after_polls = 2
    hub.external_command(KITCHEN, bottom=0, middle=100)  # app opens the vanes
    await _poll(hass, freezer, 31)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert coordinator.is_moving(str(KITCHEN), AXIS_MIDDLE)
    assert coordinator.update_interval == MOTION
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.CLOSED  # tilt motion is not lift motion

    await _poll(hass, freezer, 2)
    assert not coordinator.is_moving(str(KITCHEN), AXIS_MIDDLE)
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 100
