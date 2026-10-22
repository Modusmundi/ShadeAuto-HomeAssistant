"""Polling coordinator: fetches hub status and tracks commanded moves.

The hub is the source of truth. Each shade has two axes, the bottom rail
(lift) and the middle rail (vane tilt); for each the hub reports an actual
position and a target. A shade is *moving* while the two differ. Our own
commands are tracked as *pending* targets for a short grace period, until the
hub's reported target reflects them.

Data flow: a cover entity sends a command through :class:`ShadeAutoApi` and
calls :meth:`ShadeAutoCoordinator.register_command`. While anything is moving
the coordinator polls ``/status`` every ``motion_poll_sec`` instead of
``poll_seconds`` (the hub publishes only end states, and it does not push
notifications), and after ``verify_delay_sec`` it checks once that the shade
reached its target, retrying the command a single time if it did not and if
nobody else has re-commanded the shade in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ShadeAutoApi, ShadeAutoError
from .const import (
    ATTR_BATTERY,
    ATTR_BOTTOM_RAIL,
    ATTR_MIDDLE_RAIL,
    ATTR_PERIPHERAL_UID,
    ATTR_TARGET_BOTTOM_RAIL,
    ATTR_TARGET_MIDDLE_RAIL,
    AXES,
    AXIS_BOTTOM,
    AXIS_FIELDS,
    AXIS_MIDDLE,
    DOMAIN,
    MOTION_FAILSAFE_SEC,
    PENDING_GRACE_SEC,
    POSITION_TOLERANCE,
)
from .options import ShadeAutoOptions
from .util import to_int

_LOGGER = logging.getLogger(__name__)

# Fields copied from each /status record into the coordinator's data.
STATUS_FIELDS = (
    ATTR_BOTTOM_RAIL,
    ATTR_MIDDLE_RAIL,
    ATTR_TARGET_BOTTOM_RAIL,
    ATTR_TARGET_MIDDLE_RAIL,
    ATTR_BATTERY,
    "Name",
    "ModuleType",
    "ModuleDetail",
    "FirmwareVersion",
)


@dataclass(frozen=True, slots=True)
class Peripheral:
    """Static facts about one shade, from ``/GetAllPeripheral``."""

    uid: str
    name: str
    module_type: int | None
    module_detail: int | None

    @classmethod
    def from_hub(cls, record: dict[str, Any]) -> Peripheral:
        uid = str(record.get(ATTR_PERIPHERAL_UID))
        name = record.get("PeripheralName") or record.get("Name") or record.get("DisplayName")
        return cls(
            uid=uid,
            name=str(name) if name else f"Shade {uid}",
            module_type=to_int(record.get("ModuleType")),
            module_detail=to_int(record.get("ModuleDetail")),
        )


@dataclass
class AxisState:
    """What we know about one axis of one shade."""

    actual: int | None = None  # last position the hub reported
    target: int | None = None  # last target the hub reported
    pending: int | None = None  # target we sent that the hub has not reflected yet
    pending_ts: float | None = None  # monotonic time the pending target was sent
    changed_ts: float | None = None  # monotonic time actual/target last changed (or we sent a command)

    def at(self, value: int) -> bool:
        return self.actual is not None and abs(self.actual - value) <= POSITION_TOLERANCE


@dataclass
class ShadeState:
    """Motion bookkeeping for one shade."""

    axes: dict[str, AxisState] = field(default_factory=lambda: {axis: AxisState() for axis in AXES})
    cmd_id: int | None = None  # newest command we sent, so stale verifies can bail
    retry_attempted: bool = False
    stopped_ts: float | None = None  # monotonic time of our last stop command
    cancel_verify: CALLBACK_TYPE | None = None  # pending verify timer, if any


class ShadeAutoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the hub and keeps the per-shade status that entities render."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ShadeAutoApi, options: ShadeAutoOptions) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {api.host}",
            update_interval=timedelta(seconds=options.poll_seconds),
        )
        self.api = api
        self.options = options
        self.hub_device_id: str | None = None  # set by __init__ once the hub device exists
        self.peripherals: dict[str, Peripheral] = {}
        self._shades: dict[str, ShadeState] = {}
        self._cmd_seq = 0
        self._idle_interval = timedelta(seconds=options.poll_seconds)
        self._motion_interval = timedelta(seconds=options.motion_poll_sec)

    # --- lookups used by entities -------------------------------------------

    @property
    def identity(self) -> str:
        """Stable hub identity for unique IDs: its ThingName, else the host."""
        return self.api.thing_name or self.api.host

    def status_for(self, uid: str) -> dict[str, Any]:
        """The most recent ``/status`` record for ``uid`` (empty if unknown)."""
        status: dict[str, dict[str, Any]] = (self.data or {}).get("status", {})
        return status.get(str(uid), {})

    def _shade(self, uid: str) -> ShadeState:
        return self._shades.setdefault(str(uid), ShadeState())

    def _axis(self, uid: str, axis: str) -> AxisState:
        return self._shade(uid).axes[axis]

    def get_position(self, uid: str, axis: str) -> int | None:
        """The hub's last reported position on ``axis``."""
        return self._axis(uid, axis).actual

    def get_target(self, uid: str, axis: str) -> int | None:
        """Where ``axis`` is heading: our pending target, else the hub's target."""
        state = self._axis(uid, axis)
        return state.pending if state.pending is not None else state.target

    def is_moving(self, uid: str, axis: str) -> bool:
        """True while the hub's target differs from its actual position (or our command is pending)."""
        shade = self._shade(uid)
        state = shade.axes[axis]
        if state.pending is not None:
            return True
        if state.actual is None or state.target is None:
            return False
        if abs(state.target - state.actual) <= POSITION_TOLERANCE:
            return False
        if shade.stopped_ts is not None and (state.changed_ts is None or state.changed_ts <= shade.stopped_ts):
            # We stopped it; the hub re-targets to the stopped position only after a delay.
            return False
        # Nothing changing for a long time means a stalled or unreachable shade, not a move.
        stalled = state.changed_ts is not None and time.monotonic() - state.changed_ts > MOTION_FAILSAFE_SEC
        return not stalled

    def is_shade_moving(self, uid: str) -> bool:
        return any(self.is_moving(uid, axis) for axis in AXES)

    def any_moving(self) -> bool:
        return any(self.is_shade_moving(uid) for uid in self._shades)

    # --- polling --------------------------------------------------------------

    async def async_config_entry_first_refresh(self) -> None:
        try:
            await self.api.registration()
            records = await self.api.get_all_peripheral()
        except ShadeAutoError as err:
            raise ConfigEntryNotReady(f"Cannot reach ShadeAuto hub at {self.api.host}: {err}") from err
        self.peripherals = {p.uid: p for p in map(Peripheral.from_hub, records)}
        await super().async_config_entry_first_refresh()

    async def async_shutdown(self) -> None:
        for shade in self._shades.values():
            self._cancel_verify(shade)
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.api.status()
        except ShadeAutoError as err:
            raise UpdateFailed(f"status error: {err}") from err
        data = {"status": self._build_status_by_uid(status)}
        self._apply_poll_interval()
        return data

    def _build_status_by_uid(self, status: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Key the hub's status list by UID and feed positions into motion tracking."""
        by_uid: dict[str, dict[str, Any]] = {}
        for item in status:
            uid = str(item.get(ATTR_PERIPHERAL_UID) or "")
            if not uid:
                continue
            record = by_uid.setdefault(uid, {})
            for key in STATUS_FIELDS:
                if key in item:
                    record[key] = item[key]
            for axis, (actual_key, target_key) in AXIS_FIELDS.items():
                self._update_axis(uid, axis, to_int(record.get(actual_key)), to_int(record.get(target_key)))
        return by_uid

    def _update_axis(self, uid: str, axis: str, actual: int | None, target: int | None) -> None:
        """Record what the hub reports for one axis and reconcile it with our pending command."""
        state = self._axis(uid, axis)
        now = time.monotonic()
        if actual != state.actual or target != state.target:
            state.changed_ts = now
        state.actual, state.target = actual, target
        if state.pending is None:
            return
        if target == state.pending:
            state.pending = None  # the hub accepted our command
        elif state.pending_ts is not None and now - state.pending_ts > PENDING_GRACE_SEC:
            _LOGGER.debug("UID %s %s: hub never reflected target %s; dropping pending", uid, axis, state.pending)
            state.pending = None

    def _apply_poll_interval(self) -> None:
        """Poll fast while any shade moves, slowly otherwise."""
        wanted = self._motion_interval if self.any_moving() else self._idle_interval
        if self.update_interval != wanted:
            _LOGGER.debug("Poll interval -> %ss", wanted.total_seconds())
            self.update_interval = wanted

    # --- commands -------------------------------------------------------------

    def register_command(self, uid: str, *, bottom: int, middle: int | None) -> int:
        """Record a move we are about to send; returns its command id.

        Call it before the ``/control`` post so the pending target covers the
        send, and refresh the coordinator after the post so the next poll sees
        the hub's new target. ``middle`` is ``None`` when the command carries no
        ``MiddleRailPosition``, which the hub treats as 0.
        """
        uid = str(uid)
        shade = self._shade(uid)
        self._cmd_seq += 1
        cmd_id = shade.cmd_id = self._cmd_seq
        shade.retry_attempted = False
        shade.stopped_ts = None
        self._set_pending(shade, bottom=int(bottom), middle=middle)
        self._apply_poll_interval()

        self._cancel_verify(shade)
        if self.options.verify_enabled and self.options.verify_delay_sec > 0:

            async def _verify(_now: datetime) -> None:
                shade.cancel_verify = None
                await self.async_verify_and_retry(uid, bottom=int(bottom), middle=middle, cmd_id=cmd_id)

            shade.cancel_verify = async_call_later(self.hass, self.options.verify_delay_sec, _verify)
        return cmd_id

    def register_stop(self, uid: str) -> None:
        """Record that we told ``uid`` to stop; cancels any pending verify."""
        shade = self._shade(uid)
        self._cmd_seq += 1
        shade.cmd_id = self._cmd_seq
        shade.stopped_ts = time.monotonic()
        for state in shade.axes.values():
            state.pending = None
        self._cancel_verify(shade)
        self._apply_poll_interval()

    @staticmethod
    def _set_pending(shade: ShadeState, *, bottom: int, middle: int | None) -> None:
        now = time.monotonic()
        targets = {AXIS_BOTTOM: int(bottom), AXIS_MIDDLE: 0 if middle is None else int(middle)}
        for axis, value in targets.items():
            state = shade.axes[axis]
            state.pending = value
            state.pending_ts = now
            state.changed_ts = now

    @staticmethod
    def _cancel_verify(shade: ShadeState) -> None:
        if shade.cancel_verify is not None:
            shade.cancel_verify()
            shade.cancel_verify = None

    async def async_verify_and_retry(self, uid: str, *, bottom: int, middle: int | None, cmd_id: int) -> None:
        """Resend the command once if the shade is not where we asked.

        Runs ``verify_delay_sec`` after a command. Does nothing if a newer
        command was issued for the shade, or if the hub's own target has
        changed since (the app or a remote re-commanded it, and fighting them
        would be wrong).
        """
        uid = str(uid)
        shade = self._shade(uid)
        if shade.cmd_id != cmd_id or shade.retry_attempted:
            return
        await self.async_refresh()
        if shade.cmd_id != cmd_id:
            return

        wanted = {AXIS_BOTTOM: int(bottom), AXIS_MIDDLE: 0 if middle is None else int(middle)}
        if all(shade.axes[axis].at(value) for axis, value in wanted.items()):
            return
        bottom_axis = shade.axes[AXIS_BOTTOM]
        if bottom_axis.target is not None and bottom_axis.target != wanted[AXIS_BOTTOM]:
            _LOGGER.debug("UID %s: hub target changed to %s by someone else; not retrying", uid, bottom_axis.target)
            return

        _LOGGER.debug(
            "UID %s not at bottom=%s middle=%s (hub reports %s/%s); retrying once",
            uid,
            bottom,
            middle,
            bottom_axis.actual,
            shade.axes[AXIS_MIDDLE].actual,
        )
        shade.retry_attempted = True
        try:
            await self.api.control(uid, bottom=bottom, middle=middle)
        except ShadeAutoError as err:
            _LOGGER.warning("Retry for UID %s failed: %s", uid, err)
            return
        self._set_pending(shade, bottom=bottom, middle=middle)
        self._apply_poll_interval()
        await self.async_refresh()
