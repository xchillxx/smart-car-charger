"""Single hub device every entity belongs to — one vehicle per config entry,
so unlike surplus-load-switch there's no need for per-device sub-devices."""
from __future__ import annotations

from .const import DOMAIN


def hub_device_info(entry_id: str) -> dict:
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "Smart Car Charger",
    }
