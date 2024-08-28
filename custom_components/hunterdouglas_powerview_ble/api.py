"""Hunter Douglas PowerView BLE API."""

import asyncio
from dataclasses import dataclass
from enum import Enum
import time
from typing import Final

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import close_stale_connections, establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from homeassistant.components.cover import ATTR_CURRENT_POSITION

from .const import LOGGER, TIMEOUT

UUID_COV_SERVICE: Final = normalize_uuid_str("fdc1")
UUID_TX: Final = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"
UUID_DEV_SERVICE: Final = normalize_uuid_str("180a")
UUID_BAT_SERVICE: Final = normalize_uuid_str("180f")

ATTR_ACTIVITY: Final = "activity"


SHADE_TYPE: Final[dict[int, str]] = {
    1: "Designer Roller",
    4: "Roman",
    5: "Bottom Up",
    6: "Duette",
    10: "Duette and Applause SkyLift",
    19: "Provenance Woven Wood",
    31: "Vignette",
    32: "Vignette",
    42: "M25T Roller Blind",
    49: "AC Roller",
    52: "Banded Shades",
    53: "Sonnette",
    84: "Vignette",
}

OPEN_POSITION: Final = 100
CLOSED_POSITION: Final = 0

POWER_LEVELS: Final[dict[int, int]] = {
    4: 100,  # 4 is hardwired
    3: 100,  # 3 = 100% to 51% power remaining
    2: 50,  # 2 = 50% to 21% power remaining
    1: 20,  # 1 = 20% or less power remaining
    0: 0,  # 0 = No power remaining
}


class ShadeCmd(Enum):
    """The PowerView cover commands."""

    SET_POSITION = 0x01F7
    STOP = 0xB8F7
    ACTIVATE_SCENE = 0xBAF7


@dataclass
class DeviceInfo:
    """Dataclass holding available PowerView device information."""

    manufacturer: str = ""
    model: str = ""
    serial_nr: str = ""
    hw_rev: str = ""
    fw_rev: str = ""
    sw_rev: str = ""
    battery_level: int = 0


class PowerViewBLE:
    """Class to handle connection to PowerView remote device."""

    def __init__(self, ble_device: BLEDevice, home_key: bytes = b"") -> None:
        """Initialize device API via Bluetooth."""
        self._ble_device: Final[BLEDevice] = ble_device
        self.name: Final[str] = self._ble_device.name or "unknown"
        self._seqcnt: int = 1
        self._client: BleakClient | None = None
        self._data_event = asyncio.Event()
        self._data: bytearray
        self._info: DeviceInfo = DeviceInfo()
        # self._connect_lock = (
        #     asyncio.Lock()
        # )  # TODO: try get rid of (device_info vs. normal cmds)
        self._cmd_lock: Final = asyncio.Lock()
        self._cmd_next = None
        self._cipher: Final = (
            Cipher(algorithms.AES(home_key), modes.CTR(bytearray(16)))
            if len(home_key) == 16
            else None
        )

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    @property
    def info(self) -> DeviceInfo:
        """Return device information, e.g. SW version."""
        return self._info

    @property
    def is_connected(self) -> bool:
        """Return whether remote device is connected."""
        return self._client is not None and self._client.is_connected

    # general cmd: uint16_t cmd, uint8_t seqID, uint8_t data_len
    async def _cmd(self, cmd: ShadeCmd, data: bytearray) -> None:
        self._cmd_next = cmd
        if self._cmd_lock.locked():
            LOGGER.debug("%s: device busy, queing %s", self.name, cmd.name)
            return

        async with self._cmd_lock:
            try:
                await self._connect()
                assert self._client is not None, "missing BT client"
                cmd_run = self._cmd_next
                tx_data = (
                    bytearray(
                        int.to_bytes(cmd_run.value, 2, byteorder="little")
                        + bytes([self._seqcnt, len(data)])
                    )
                    + data
                )
                if self._cipher is not None:
                    enc = self._cipher.encryptor()
                    tx_data = enc.update(tx_data) + enc.finalize()
                self._data_event.clear()
                LOGGER.debug("sending cmd: %s", tx_data)
                await self._client.write_gatt_char(UUID_TX, tx_data, False)
                self._seqcnt += 1
                LOGGER.debug("waiting for response")
                try:
                    await asyncio.wait_for(self._wait_event(), timeout=TIMEOUT)
                    self._verify_response(self._data, self._seqcnt - 1, cmd_run)
                except TimeoutError as ex:
                    raise TimeoutError("Device did not send confirmation.") from ex
                # finally:
                #     await self._client.disconnect() # device disconnects itself
            except Exception as ex:
                LOGGER.debug("Error: %s - %s", type(ex).__name__, ex)
                raise

    @staticmethod
    def dec_manufacturer_data(data: bytearray) -> list[tuple[str, float]]:
        """Decode manufacturer data from BLE advertisement V2."""
        if len(data) != 9:
            LOGGER.debug("not a V2 record!")
            return []
        pos = int.from_bytes(data[3:5], byteorder="little")
        return [
            (ATTR_CURRENT_POSITION, ((pos >> 2) / 10)),
            ("home_id", int.from_bytes(data[0:2], byteorder="little")),
            ("type_id", int.from_bytes(data[2:3])),
            ("is_opening", bool(pos & 0x3 == 0x2)),
            ("is_closing", bool(pos & 0x3 == 0x1)),
            ("battery_charging", bool(pos & 0x3 == 0x3)),  # observed
            ("battery_level", POWER_LEVELS[(data[8] >> 6)]),  # cannot hit 4
            ("resetMode", bool(data[8] & 0x1)),
            ("resetClock", bool(data[8] & 0x2)),
        ]

    # position cmd: uint16_t pos1, uint16_t pos2, uint16_t pos3, uint16_t tilt, uint8_t velocity
    async def set_position(self, value: int) -> None:
        """Set position of device."""
        LOGGER.debug("%s setting position to %i", self.name, value)
        await self._cmd(
            ShadeCmd.SET_POSITION,
            bytearray(
                int.to_bytes(value * 100, 2, byteorder="little")
                + bytes([0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0x0])
            ),
        )

    async def stop(self) -> None:
        """Stop device movement."""
        LOGGER.debug("%s stop", self.name)
        await self._cmd(ShadeCmd.STOP, bytearray(b""))

    # uint8_t scene#, uint8_t unknown
    # open: scene 2 (programmed by app?)
    # close: scene 3 (programmed by app?)
    async def activate_scene(self, idx: int) -> None:
        """Activate stored scene."""
        LOGGER.debug("%s set scene #%i", self.name, idx)
        await self._cmd(
            ShadeCmd.ACTIVATE_SCENE,
            bytearray(int.to_bytes(idx, 1, byteorder="little") + bytes([0xA2])),
        )

    def _verify_response(self, input: bytearray, seq_nr: int, cmd: ShadeCmd) -> bool:
        """Verify shade response data."""
        data = input
        if self._cipher is not None:
            dec = self._cipher.decryptor()
            data = dec.update(input) + dec.finalize()
        if len(data) < 4:
            LOGGER.warning("Message too short")
            return False
        if int.from_bytes(data[0:2], byteorder="little") != cmd.value & 0xFFEF:
            LOGGER.warning("Response to wrong command")
            return False
        if int(data[2]) != seq_nr:
            LOGGER.warning("Wrong sequence number")
            return False
        if int(data[3]) != 1:
            LOGGER.warning("Wrong response data length")
            return False
        if int(data[4] != 0):
            LOGGER.warning("Return code type error")
            return False
        return True

    async def query_dev_info(self) -> dict[str, str]:
        """Return detailed device information."""
        data: dict[str, str] = {}
        uuids: Final[dict[str, str]] = {
            "manufacturer": "2a29",
            "model": "2a24",
            "serial_nr": "2a25",
            "hw_rev": "2a27",
            "fw_rev": "2a26",
            "sw_rev": "2a28",
        }

        try:
            await self._connect()
            assert self._client is not None

            for key, uuid in uuids.items():
                LOGGER.debug("querying %s(%s)", key, uuid)
                data[key] = (
                    (await self._client.read_gatt_char(normalize_uuid_str(uuid)))
                    .copy()
                    .decode("UTF-8")
                )
        except BleakError as ex:
            LOGGER.debug("%s error: %s", self.name, ex)
            return {}
        finally:
            await self._disconnect()
        LOGGER.debug("%s device data: %s", self.name, data)
        return data

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback function."""

        LOGGER.debug("Disconnected from %s", client.address)

    def _notification_handler(self, _sender, data: bytearray) -> None:
        LOGGER.debug("%s received BLE data: %s", self.name, data)
        self._data = data
        self._data_event.set()

    async def _connect(self) -> None:
        """Connect to the device and setup notification if not connected."""

        LOGGER.debug("Connecting %s", self.name)

        if self.is_connected:
            LOGGER.debug("%s already connected", self.name)
            return

        start = time.time()
        await close_stale_connections(self._ble_device)
        LOGGER.debug("%s: closing stale connections took %is", self.name, time.time()-start)
        self._client = await establish_connection(
            BleakClient,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnect,
            services=[
                UUID_COV_SERVICE,
                # self.UUID_DEV_SERVICE,
                # self.UUID_BAT_SERVICE,
            ],
        )
        await self._client.start_notify(UUID_TX, self._notification_handler)

        LOGGER.debug("\tconnect took %is", time.time() - start)

        # await self._query_dev_info()

    async def _disconnect(self) -> None:
        """Disconnect the device and stop notifications."""

        if self._client is not None and self.is_connected:
            LOGGER.debug("Disconnecting device %s", self.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                LOGGER.warning("Disconnect failed!")
