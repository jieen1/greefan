import asyncio
import enum
import logging
import re
from enum import IntEnum, unique

import greefan.network as network
from greefan.exceptions import DeviceNotBoundError, DeviceTimeoutError


class Props(enum.Enum):
    POWER = "Pow"
    MODE = "Mod"

    FAN_SPEED = "WdSpd"
    SWING_HORIZ = "SwingLfRig"
    SWING_VERT = "SwUpDn"


@unique
class Mode(IntEnum):
    Auto = 0
    Sleep = 1


@unique
class FanSpeed(IntEnum):
    One = 1
    Two = 2
    Three = 3
    Four = 4
    Five = 5
    Six = 6
    Seven = 7
    Eight = 8
    Nine = 9
    Ten = 10
    Eleven = 11
    Twelve = 12


@unique
class HorizontalSwing(IntEnum):
    Off = 0
    D60 = 1
    D100 = 2
    D360 = 3


@unique
class VerticalSwing(IntEnum):
    Default = 0


class DeviceInfo:
    """Device information class, used to identify and connect

    Attributes
        ip: IP address (ipv4 only) of the physical device
        port: Usually this will always be 7000
        mac: mac address, in the format 'aabbcc112233'
        name: Name of unit, if available
    """

    def __init__(self, ip, port, mac, name, brand=None, model=None, version=None):
        self.ip = ip
        self.port = port
        self.mac = mac
        self.name = name if name else mac.replace(":", "")
        self.brand = brand
        self.model = model
        self.version = version

    def __str__(self):
        return f"Device: {self.name} @ {self.ip}:{self.port} (mac: {self.mac})"

    def __eq__(self, other):
        """Check equality based on Device Info properties"""
        if isinstance(other, DeviceInfo):
            return (
                    self.mac == other.mac
                    and self.name == other.name
                    and self.brand == other.brand
                    and self.model == other.model
                    and self.version == other.version
            )
        return False

    def __ne__(self, other):
        """Check inequality based on Device Info properties"""
        return not self.__eq__(other)


class Device:
    """Class representing a physical device, it's state and properties.

    Devices must be bound, either by discovering their presence, or supplying a persistent
    device key which is then used for communication (and encryption) with the unit. See the
    `bind` function for more details on how to do this.

    Once a device is bound occasionally call `update_state` to request and update state from
    the HVAC, as it is possible that it changes state from other sources.

    Attributes:
        power: A boolean indicating if the unit is on or off
        mode: An int indicating operating mode, see `Mode` enum for possible values
        fan_speed: An int indicating fan speed, see `FanSpeed` enum for possible values
        horizontal_swing: An int to control the horizontal blade position, see `HorizontalSwing` enum for possible values
        vertical_swing: An int to control the vertical blade position, see `VerticalSwing` enum for possible values
    """

    def __init__(self, device_info):
        self._logger = logging.getLogger(__name__)

        self.device_info = device_info
        self.device_key = None

        """ Device properties """
        self.hid = None
        self.version = None
        self._properties = None
        self._dirty = []

    async def bind(self, key=None):
        """Run the binding procedure.

        Binding is a finnicky procedure, and happens in 1 of 2 ways:
            1 - Without the key, binding must pass the device info structure immediately following
                the search devices procedure. There is only a small window to complete registration.
            2 - With a key, binding is implicit and no further action is required

            Both approaches result in a device_key which is used as like a persitent session id.

        Args:
            key (str): The device key, when provided binding is a NOOP, if None binding will
                       attempt to negatiate the key with the device.

        Raises:
            DeviceNotBoundError: If binding was unsuccessful and no key returned
            DeviceTimeoutError: The device didn't respond
        """

        if not self.device_info:
            raise DeviceNotBoundError

        self._logger.info("Starting device binding to %s", str(self.device_info))

        try:
            if key:
                self.device_key = key
            else:
                self.device_key = await network.bind_device(
                    self.device_info, announce=False
                )
        except asyncio.TimeoutError:
            raise DeviceTimeoutError

        if not self.device_key:
            raise DeviceNotBoundError
        else:
            self._logger.info("Bound to device using key %s", self.device_key)

    async def request_version(self) -> None:
        """Request the firmware version from the device."""
        ret = await network.request_state(["hid", 'time'], self.device_info, self.device_key)
        self.hid = ret.get("hid")

        # Ex: hid = 362001000762+U-CS532AE(LT)V3.31.bin
        if self.hid:
            match = re.search(r"(?<=V)([\d.]+)\.bin$", self.hid)
            self.version = match and match.group(1)

    async def update_state(self):
        """Update the internal state of the device structure of the physical device"""
        if not self.device_key:
            await self.bind()

        self._logger.debug("Updating device properties for (%s)", str(self.device_info))

        props = [x.value for x in Props]

        try:
            self._properties = await network.request_state(
                props, self.device_info, self.device_key
            )

            # This check should prevent need to do version & device overrides
            # to correctly compute the temperature. Though will need to confirm
            # that it resolves all possible cases.
            if not self.hid:
                await self.request_version()

        except asyncio.TimeoutError:
            raise DeviceTimeoutError

    async def push_state_update(self):
        """Push any pending state updates to the unit"""
        if not self._dirty:
            return

        if not self.device_key:
            await self.bind()

        self._logger.debug("Pushing state updates to (%s)", str(self.device_info))

        props = {}
        for name in self._dirty:
            value = self._properties.get(name)
            self._logger.debug("Sending remote state update %s -> %s", name, value)
            props[name] = value

        self._dirty.clear()

        try:
            await network.send_state(props, self.device_info, key=self.device_key)
        except asyncio.TimeoutError:
            raise DeviceTimeoutError

    def get_property(self, name):
        """Generic lookup of properties tracked from the physical device"""
        if self._properties:
            return self._properties.get(name.value)
        return None

    def set_property(self, name, value):
        """Generic setting of properties for the physical device"""
        if not self._properties:
            self._properties = {}

        if self._properties.get(name.value) == value:
            return
        else:
            self._properties[name.value] = value
            if name.value not in self._dirty:
                self._dirty.append(name.value)

    @property
    def power(self) -> bool:
        return bool(self.get_property(Props.POWER))

    @power.setter
    def power(self, value: int):
        self.set_property(Props.POWER, int(value))

    @property
    def mode(self) -> int:
        return self.get_property(Props.MODE)

    @mode.setter
    def mode(self, value: int):
        self.set_property(Props.MODE, int(value))

    @property
    def fan_speed(self) -> int:
        return self.get_property(Props.FAN_SPEED)

    @fan_speed.setter
    def fan_speed(self, value: int):
        self.set_property(Props.FAN_SPEED, int(value))

    @property
    def horizontal_swing(self) -> int:
        return self.get_property(Props.SWING_HORIZ)

    @horizontal_swing.setter
    def horizontal_swing(self, value: int):
        self.set_property(Props.SWING_HORIZ, int(value))

    @property
    def vertical_swing(self) -> int:
        return self.get_property(Props.SWING_VERT)

    @vertical_swing.setter
    def vertical_swing(self, value: int):
        self.set_property(Props.SWING_VERT, int(value))
