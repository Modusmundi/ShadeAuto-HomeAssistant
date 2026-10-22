"""Cover entities: one per shade, with lift on the bottom rail and vane tilt on the middle rail.

Position and tilt come from the hub. Because the hub reports only end states,
the entity interpolates the position slider during a lift ("estimation") from
the last known position to the target over ``full_travel_time_sec``; the
estimate is discarded the moment the coordinator no longer considers the
bottom rail to be moving, so the hub always wins once a move has settled.

Hub rules that shape the commands (see ``docs/HUB_PROTOCOL.md``):

* every command must carry ``BottomRailPosition``; leaving out
  ``MiddleRailPosition`` makes the hub close the vanes;
* vanes are honoured only while the shade is fully lowered, so a tilt command
  is sent as ``(bottom 0, middle tilt)`` and lowers the shade if needed, the
  same way Home Assistant handles Silhouette-style shades elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShadeAutoError, clamp_position
from .const import AXIS_BOTTOM, AXIS_MIDDLE, DOMAIN
from .coordinator import ShadeAutoCoordinator
from .entity import ShadeAutoEntity

if TYPE_CHECKING:
    from . import ShadeAutoConfigEntry

_LOGGER = logging.getLogger(__name__)

LIFT_FEATURES = (
    CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP
)
TILT_FEATURES = (
    CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.SET_TILT_POSITION
    | CoverEntityFeature.STOP_TILT
)

# Slider animation refresh bounds: fast enough to look smooth, slow enough not to flood the state machine.
_ANIMATION_MIN_INTERVAL = 0.1
_ANIMATION_MAX_INTERVAL = 0.5
_ANIMATION_FRAMES = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ShadeAutoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(ShadeAutoCover(coordinator, uid) for uid in coordinator.peripherals)


class ShadeAutoCover(ShadeAutoEntity, CoverEntity):
    """A shade as a Home Assistant cover: lift plus, on vane shades, tilt."""

    _attr_name = None  # the main entity carries the device name
    _attr_device_class = CoverDeviceClass.SHADE

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid)
        self._has_tilt = coordinator.options.tilt_enabled
        self._attr_supported_features = LIFT_FEATURES | (TILT_FEATURES if self._has_tilt else CoverEntityFeature(0))
        # Estimation of the current lift: start position, target, start time, duration.
        self._est_start_pos: float | None = None
        self._est_target: float | None = None
        self._est_start_ts: float | None = None
        self._est_travel_time_s: float | None = None
        self._anim_task: asyncio.Task[None] | None = None
        self._removed = False

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._cancel_animation()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.is_moving(self._uid, AXIS_BOTTOM):
            self._clear_estimation()
        super()._handle_coordinator_update()

    # --- estimation (bottom rail only) ------------------------------------------

    def _estimation_active(self) -> bool:
        """True while a lift is being interpolated and the coordinator still sees it moving."""
        if not self.coordinator.options.estimation_enabled:
            return False
        if self._est_start_pos is None or self._est_target is None or self._est_start_ts is None:
            return False
        if self._est_travel_time_s is None or self._est_travel_time_s <= 0:
            return False
        if not self.coordinator.is_moving(self._uid, AXIS_BOTTOM):
            return False
        return time.monotonic() - self._est_start_ts < self._est_travel_time_s

    def _estimated_position(self) -> int | None:
        """Interpolated lift position during a move; the hub's value otherwise."""
        hub_position = self.coordinator.get_position(self._uid, AXIS_BOTTOM)
        if not self.coordinator.is_moving(self._uid, AXIS_BOTTOM):
            self._clear_estimation()
            return hub_position
        if (
            not self.coordinator.options.estimation_enabled
            or self._est_start_pos is None
            or self._est_target is None
            or self._est_start_ts is None
        ):
            return hub_position
        if not self._est_travel_time_s or self._est_travel_time_s <= 0:
            return round(self._est_target)
        elapsed = max(0.0, time.monotonic() - self._est_start_ts)
        if elapsed >= self._est_travel_time_s:
            return round(self._est_target)
        ratio = min(elapsed / self._est_travel_time_s, 1.0)
        return round(self._est_start_pos + (self._est_target - self._est_start_pos) * ratio)

    def _start_estimation(self, target: int) -> None:
        """Begin interpolating the lift toward ``target`` from where the shade is believed to be."""
        options = self.coordinator.options
        if not options.estimation_enabled:
            self._clear_estimation()
            return
        now = time.monotonic()
        start_pos: float | None = None
        # A command issued mid-move starts from the interpolated position, not the old baseline.
        if (
            self._est_start_pos is not None
            and self._est_target is not None
            and self._est_start_ts is not None
            and self._est_travel_time_s
        ):
            ratio_prev = min(max(0.0, now - self._est_start_ts) / self._est_travel_time_s, 1.0)
            start_pos = self._est_start_pos + (self._est_target - self._est_start_pos) * ratio_prev
        if start_pos is None:
            start_pos = float(self.coordinator.get_position(self._uid, AXIS_BOTTOM) or 0)

        self._est_start_pos = start_pos
        self._est_target = float(target)
        self._est_start_ts = now
        distance = abs(target - start_pos)
        self._est_travel_time_s = options.full_travel_time_sec * distance / 100.0 if distance > 0 else 0.0
        _LOGGER.debug("UID %s estimation: %.1f -> %s over %.1fs", self._uid, start_pos, target, self._est_travel_time_s)

    def _clear_estimation(self) -> None:
        self._est_start_pos = None
        self._est_target = None
        self._est_start_ts = None
        self._est_travel_time_s = None

    def _start_animation(self) -> None:
        """Restart the task that re-renders the slider during the estimated move."""
        self._cancel_animation()
        if not self._estimation_active():
            return
        self._anim_task = self.coordinator.config_entry.async_create_background_task(
            self.hass, self._animate_slider(), name=f"{DOMAIN} slider {self._uid}"
        )

    def _cancel_animation(self) -> None:
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    async def _animate_slider(self) -> None:
        """Write state a few times per second until the estimated move completes."""
        assert self._est_travel_time_s  # guaranteed by _start_animation
        interval = max(
            _ANIMATION_MIN_INTERVAL, min(_ANIMATION_MAX_INTERVAL, self._est_travel_time_s / _ANIMATION_FRAMES)
        )
        try:
            while self._estimation_active():
                self.async_write_ha_state()
                await asyncio.sleep(interval)
        finally:
            if not self._removed:
                # One last write so the slider ends exactly on the target.
                self.async_write_ha_state()

    # --- state ------------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        return self._estimated_position()

    @property
    def current_cover_tilt_position(self) -> int | None:
        if not self._has_tilt:
            return None
        return self.coordinator.get_position(self._uid, AXIS_MIDDLE)

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    def _lift_direction(self) -> int:
        """+1 while lifting toward a higher position, -1 while lowering, 0 when not moving."""
        if not self.coordinator.is_moving(self._uid, AXIS_BOTTOM):
            return 0
        target = self.coordinator.get_target(self._uid, AXIS_BOTTOM)
        actual = self.coordinator.get_position(self._uid, AXIS_BOTTOM)
        if target is None or actual is None or target == actual:
            return 0
        return 1 if target > actual else -1

    @property
    def is_opening(self) -> bool | None:
        return self._lift_direction() > 0

    @property
    def is_closing(self) -> bool | None:
        return self._lift_direction() < 0

    # --- commands ---------------------------------------------------------------

    async def _async_command(self, *, bottom: int, middle: int) -> None:
        """Send one composite move and start the optimistic UI update."""
        bottom, middle = clamp_position(bottom), clamp_position(middle)
        if bottom > 0 and middle > 0:
            # The hub would accept it and then reset the vanes anyway; be explicit instead.
            middle = 0
        self._start_estimation(bottom)
        self.coordinator.register_command(self._uid, bottom=bottom, middle=middle)
        self._start_animation()
        self.async_write_ha_state()
        try:
            await self.coordinator.api.control(self._uid, bottom=bottom, middle=middle)
        except ShadeAutoError as err:
            raise HomeAssistantError(f"Could not move shade {self.name}: {err}") from err
        # Read the hub right away: its target now reflects the command, and the
        # refresh re-arms the poll timer at the faster motion interval.
        await self.coordinator.async_refresh()

    def _current_tilt_target(self) -> int:
        target = self.coordinator.get_target(self._uid, AXIS_MIDDLE)
        if target is None:
            target = self.coordinator.get_position(self._uid, AXIS_MIDDLE)
        return target or 0

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = clamp_position(kwargs[ATTR_POSITION])
        # Fully lowering keeps the vanes as they are; any other height closes them (hub rule).
        middle = self._current_tilt_target() if position == 0 else 0
        await self._async_command(bottom=position, middle=middle)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_command(bottom=100, middle=0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=self._current_tilt_target())

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=clamp_position(kwargs[ATTR_TILT_POSITION]))

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=100)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=0)

    async def _async_stop(self) -> None:
        self._clear_estimation()
        self._cancel_animation()
        self.coordinator.register_stop(self._uid)
        self.async_write_ha_state()
        try:
            await self.coordinator.api.stop(self._uid)
        except ShadeAutoError as err:
            raise HomeAssistantError(f"Could not stop shade {self.name}: {err}") from err
        await self.coordinator.async_refresh()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._async_stop()

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_stop()
