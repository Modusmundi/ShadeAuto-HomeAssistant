"""Typed view of the config entry options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_ESTIMATION_ENABLED,
    CONF_FULL_TRAVEL_TIME_SEC,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_MOTION_POLL_SEC,
    CONF_POLL_SECONDS,
    CONF_SEND_SPACING_SEC,
    CONF_TILT_ENABLED,
    CONF_VERIFY_DELAY_SEC,
    CONF_VERIFY_ENABLED,
    DEFAULT_ESTIMATION_ENABLED,
    DEFAULT_FULL_TRAVEL_TIME,
    DEFAULT_LOW_BATT,
    DEFAULT_MOTION_POLL,
    DEFAULT_POLL,
    DEFAULT_SEND_SPACING,
    DEFAULT_TILT_ENABLED,
    DEFAULT_VERIFY_DELAY,
    DEFAULT_VERIFY_ENABLED,
)


@dataclass(frozen=True, slots=True)
class ShadeAutoOptions:
    """Options from the integration's Configure dialog, with defaults applied.

    Parsed once per config entry load (the options flow reloads the entry when
    anything changes), so every consumer reads typed attributes instead of
    re-reading ``entry.options`` with its own fallbacks. Unknown keys, such as
    options from older versions, are ignored.
    """

    poll_seconds: int = DEFAULT_POLL
    motion_poll_sec: float = DEFAULT_MOTION_POLL
    low_battery_threshold: int = DEFAULT_LOW_BATT
    send_spacing_sec: float = DEFAULT_SEND_SPACING
    verify_enabled: bool = DEFAULT_VERIFY_ENABLED
    verify_delay_sec: float = DEFAULT_VERIFY_DELAY
    estimation_enabled: bool = DEFAULT_ESTIMATION_ENABLED
    full_travel_time_sec: float = DEFAULT_FULL_TRAVEL_TIME
    tilt_enabled: bool = DEFAULT_TILT_ENABLED

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any]) -> ShadeAutoOptions:
        """Build from ``entry.options``."""
        return cls(
            poll_seconds=int(options.get(CONF_POLL_SECONDS, DEFAULT_POLL)),
            motion_poll_sec=float(options.get(CONF_MOTION_POLL_SEC, DEFAULT_MOTION_POLL)),
            low_battery_threshold=int(options.get(CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATT)),
            send_spacing_sec=float(options.get(CONF_SEND_SPACING_SEC, DEFAULT_SEND_SPACING)),
            verify_enabled=bool(options.get(CONF_VERIFY_ENABLED, DEFAULT_VERIFY_ENABLED)),
            verify_delay_sec=float(options.get(CONF_VERIFY_DELAY_SEC, DEFAULT_VERIFY_DELAY)),
            estimation_enabled=bool(options.get(CONF_ESTIMATION_ENABLED, DEFAULT_ESTIMATION_ENABLED)),
            full_travel_time_sec=float(options.get(CONF_FULL_TRAVEL_TIME_SEC, DEFAULT_FULL_TRAVEL_TIME)),
            tilt_enabled=bool(options.get(CONF_TILT_ENABLED, DEFAULT_TILT_ENABLED)),
        )
