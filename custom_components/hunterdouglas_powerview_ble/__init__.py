"""The Hunter Douglas PowerView (BLE) integration."""

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator

PLATFORMS: list[Platform] = [Platform.COVER]

type ConfigEntryType = ConfigEntry[PVCoordinator]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Set up BT Battery Management System from a config entry."""
    LOGGER.debug("Setup of %s", repr(entry))

    if entry.unique_id is None:
        raise ConfigEntryError("Missing unique ID for device.")

    ble_device = async_ble_device_from_address(
        hass=hass, address=entry.unique_id, connectable=True
    )

    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find PowerView device ({entry.unique_id}) via Bluetooth"
        )

    coordinator = PVCoordinator(hass, ble_device)
    # Query the device the first time, initialise coordinator.data
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # Ignore, e.g. timeouts, to gracefully handle connection issues
        LOGGER.warning("Failed to initialize PowerView device %s, continuing", ble_device.name)

    # Insert the coordinator in the global registry
    hass.data.setdefault(DOMAIN, {})
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, str(unload_ok))
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntryType) -> bool:
    """Migrate old entry."""

    if config_entry.version > 1:
        # This means the user has downgraded from a future version
        LOGGER.debug("Cannot downgrade from version %s", config_entry.version)
        return False

    LOGGER.debug("Migrating from version %s", config_entry.version)

    return False
