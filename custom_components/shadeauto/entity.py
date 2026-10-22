"""Base class shared by every entity that belongs to one shade."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import Peripheral, ShadeAutoCoordinator

# Module type/detail pairs the hub reports for shade families we recognise.
KNOWN_MODELS = {(48, 3): "PerfectSheer"}


def model_name(peripheral: Peripheral) -> str:
    """A readable model string for the device page."""
    key = (peripheral.module_type, peripheral.module_detail)
    if key in KNOWN_MODELS:
        return KNOWN_MODELS[key]
    if peripheral.module_type is None:
        return "Shade"
    return f"Shade (type {peripheral.module_type}/{peripheral.module_detail})"


class ShadeAutoEntity(CoordinatorEntity[ShadeAutoCoordinator]):
    """An entity attached to one shade (hub peripheral).

    Entity names follow Home Assistant's device-name convention: the shade's
    device carries the hub's name for it, the main cover entity has no name of
    its own, and the diagnostic entities add a translated suffix.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str, *, unique_id_suffix: str = "") -> None:
        super().__init__(coordinator)
        self._uid = str(uid)
        self._attr_unique_id = f"shadeauto_{coordinator.identity}_{uid}{unique_id_suffix}"
        peripheral = coordinator.peripherals[self._uid]
        host = coordinator.api.host
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"shade_{host}_{self._uid}")},
            name=peripheral.name,
            manufacturer=MANUFACTURER,
            model=model_name(peripheral),
        )
        firmware = coordinator.status_for(self._uid).get("FirmwareVersion")
        if firmware:
            self._attr_device_info["sw_version"] = str(firmware)
        if coordinator.hub_device_id is not None:
            self._attr_device_info["via_device_id"] = coordinator.hub_device_id

    @property
    def available(self) -> bool:
        """Available while the hub keeps reporting this shade."""
        return super().available and self._uid in self.coordinator.data.get("status", {})

    @property
    def _status(self) -> dict[str, Any]:
        """This shade's most recent record from ``/status``."""
        return self.coordinator.status_for(self._uid)
