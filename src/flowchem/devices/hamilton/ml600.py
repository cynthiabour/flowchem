"""Control Hamilton ML600 syringe pump via the protocol1/RNO+."""
from __future__ import annotations

import string
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
import asyncio

import aioserial
from loguru import logger

from flowchem import ureg
from flowchem.components.device_info import DeviceInfo
from flowchem.devices.flowchem_device import FlowchemDevice
from flowchem.devices.hamilton.ml600_pump import ML600Pump
from flowchem.devices.hamilton.ml600_valve import ML600LeftValve, ML600RightValve
from flowchem.utils.exceptions import InvalidConfigurationError
from flowchem.utils.people import dario, jakob, wei_hsin

if TYPE_CHECKING:
    import pint


# i.e. PUMP_ADDRESS = {1: 'a', 2: 'b', 3: 'c', 4: 'd', ..., 16: 'p'}
# Note ':' is used for broadcast within the daisy chain.
PUMP_ADDRESS = dict(enumerate(string.ascii_lowercase[:16], start=1))


@dataclass
class Protocol1Command:
    """Class representing a pump command and its expected reply."""

    command: str
    target_pump_num: int = 1
    target_component: str = ""
    command_value: str = ""
    optional_parameter: str = ""
    parameter_value: str = ""
    execution_command: str = "R"  # Execute

    def compile(self) -> str:
        """Create actual command byte by prepending pump address to command and appending executing command."""
        compiled_command = (
            f"{PUMP_ADDRESS[self.target_pump_num]}"
            f"{self.target_component}"
            f"{self.command}{self.command_value}"
        )

        if self.parameter_value:
            compiled_command += f"{self.optional_parameter}{self.parameter_value}"

        return compiled_command + self.execution_command


class HamiltonPumpIO:
    """Setup with serial parameters, low level IO."""

    ACKNOWLEDGE = chr(6)
    NEGATIVE_ACKNOWLEDGE = chr(21)
    DEFAULT_CONFIG = {
        "timeout": 0.1,
        "baudrate": 9600,
        "parity": aioserial.PARITY_EVEN,
        "stopbits": aioserial.STOPBITS_ONE,
        "bytesize": aioserial.SEVENBITS,
    }

    def __init__(self, aio_port: aioserial.Serial) -> None:
        """Initialize serial port, not pumps."""
        self._serial = aio_port
        self.num_pump_connected: int | None = (
            None  # Set by `HamiltonPumpIO.initialize()`
        )

    @classmethod
    def from_config(cls, config):
        """Create HamiltonPumpIO from config."""
        configuration = HamiltonPumpIO.DEFAULT_CONFIG | config

        try:
            serial_object = aioserial.AioSerial(**configuration)
        except aioserial.SerialException as serial_exception:
            raise InvalidConfigurationError(
                f"Cannot connect to the pump on the port <{configuration.get('port')}>"
            ) from serial_exception

        return cls(serial_object)

    async def initialize(self, hw_initialization: bool = True):
        """Ensure connection with pump and initialize it (if hw_initialization is True)."""
        self.num_pump_connected = await self._assign_pump_address()
        if hw_initialization:
            await self._hw_init()

    async def _assign_pump_address(self) -> int:
        """Auto assign pump addresses.

        To be run on init, auto assign addresses to pumps based on their position in the daisy chain.
        A custom command syntax with no addresses is used here so read and write has been rewritten
        """
        try:
            # the command for an unknown reason often replies wrongly on first attempt. therefore, this is done twice
            await self._write_async(b"1a\r")
            await self._read_reply_async()
            await self._write_async(b"1a\r")
        except aioserial.SerialException as e:
            raise InvalidConfigurationError from e

        reply = await self._read_reply_async()
        if not reply or reply[:1] != "1":
            raise InvalidConfigurationError(f"No pump found on {self._serial.port}")
        # reply[1:2] should be the address of the last pump. However, this does not work reliably.
        # So here we enumerate the pumps explicitly instead
        last_pump = 0
        for pump_num, address in PUMP_ADDRESS.items():
            await self._write_async(f"{address}UR\r".encode("ascii"))
            if "NV01" in await self._read_reply_async():
                last_pump = pump_num
            else:
                break
        logger.debug(f"Found {last_pump} pumps on {self._serial.port}!")
        return int(last_pump)

    async def _hw_init(self):
        """Send to all pumps the HW initialization command (i.e. homing)."""
        await self._write_async(b":XR\r")  # Broadcast: initialize + execute
        # Note: no need to consume reply here because there is none (since we are using broadcast)

    async def _write_async(self, command: bytes):
        """Write a command to the pump."""
        await self._serial.write_async(command)
        logger.debug(f"Command {command!r} sent!")

    async def _read_reply_async(self) -> str:
        """Read the pump reply from serial communication."""
        reply_string = await self._serial.readline_async()
        logger.debug(f"Reply received: {reply_string}")
        return reply_string.decode("ascii")

    def parse_response(self, response: str) -> str:
        """Split a received line in its components: status, reply."""
        status, reply = response[:1], response[1:]

        assert status in (self.ACKNOWLEDGE, self.NEGATIVE_ACKNOWLEDGE)
        if status == self.NEGATIVE_ACKNOWLEDGE:
            logger.warning("Negative acknowledge received")
            warnings.warn(
                "Negative acknowledge reply: check command syntax!",
                stacklevel=2,
            )

        return reply.rstrip()  # removes trailing <cr>

    async def write_and_read_reply_async(self, command: Protocol1Command) -> str:
        """Send a command to the pump, read the replies and returns it, optionally parsed."""
        self._serial.reset_input_buffer()
        await self._write_async(f"{command.compile()}\r".encode("ascii"))
        response = await self._read_reply_async()

        if not response:
            raise InvalidConfigurationError(
                f"No response received from pump! "
                f"Maybe wrong pump address? (Set to {command.target_pump_num})"
            )

        return self.parse_response(response)

class ML600Commands(Enum):
    """ Just a collection of commands. Grouped here to ease future, unlikely, changes. """

    PAUSE = "K"
    RESUME = "$"
    CLEAR_BUFFER = "V"

    INIT_ALL = "X"
    INIT_VALVE_ONLY = "LX"
    INIT_SYRINGE_ONLY = "X1"

    # only works for pumps with two syringe drivers
    SET_VALVE_CONTINUOUS_DISPENSE = "LST19"
    SET_VALVE_DUAL_DILUTOR = "LST20"

    # if there are two drivers, both sides can be selected
    SELECT_LEFT = "B"
    SELECT_RIGHT = "C"

    # SYRINGE POSITION
    PICKUP = "P"
    DELIVER = "D"
    ABSOLUTE_MOVE = "M"

    # VALVE POSITION
    # strongly discouraged since mapping changes
    VALVE_TO_INLET = "I"
    VALVE_TO_OUTLET = "O"
    VALVE_TO_WASH = "W"
    VALVE_BY_NAME_CW = "LP0"
    VALVE_BY_NAME_CCW = "LP1"
    # strongly encouraged since mapping is clear if initial/0 position is clear and rotor/stator are known
    VALVE_BY_ANGLE_CW = "LA0"
    VALVE_BY_ANGLE_CCW = "LA1"

    # STATUS REQUEST
    # INFORMATION REQUEST -- these all returns Y/N/* where * means busy
    REQUEST_DONE = "F"
    SYRINGE_HAS_ERROR = "Z"
    VALVE_HAS_ERROR = "G"
    IS_SINGLE_SYRINGE = "H"
    # STATUS REQUEST  - these have complex responses, see relevant methods for details.
    STATUS_REQUEST = "E1"
    ERROR_REQUEST = "E2"
    TIMER_REQUEST = "E3"
    BUSY_STATUS = "T1"
    ERROR_STATUS = "T2"
    # PARAMETER REQUEST
    SYRINGE_DEFAULT_SPEED = "YQS"
      # 2-3692 seconds per stroke
    CURRENT_SYRINGE_POSITION = "YQP"  # 0-52800 steps
    SYRINGE_DEFAULT_BACKOFF = "YQB"  # 0-1000 steps
    CURRENT_VALVE_POSITION = "LQP"
      # 1-8 (see docs, Table 3.2.2
    GET_RETURN_STEPS = "YQN"  # 0-1000 steps
    # PARAMETER CHANGE
    SET_RETURN_STEPS = "YSN"  # 0-1000
    # VALVE REQUEST
    VALVE_ANGLE = "LQA"  # 0-359 degrees
    VALVE_CONFIGURATION = "YQS"
      # 11-20 (see docs, Table 3.2.2
    #Set valve speed
    SET_VALVE_SPEED = "LSF"  # 15-720 degrees per sec
    #Set valve speed
    GET_VALVE_SPEED = "LQF"
    # TIMER REQUEST
    TIMER_DELAY = "<T"  # 0–99999999 ms
    # FIRMWARE REQUEST
    # TODO this is wrong, get correct
    FIRMWARE_VERSION = "U"
      # xxii.jj.k (ii major, jj minor, k revision)
    OPTIONAL_PARAMETER = "S"

class ML600(FlowchemDevice):
    """ML600 implementation according to manufacturer docs. Tested on a 61501-01 (i.e. single syringe system).

    From manufacturer docs:
    To determine the volume dispensed per step the total syringe volume is divided by
    48,000 steps. All Hamilton instrument syringes are designed with a 60 mm stroke
    length and the Microlab 600 is designed to move 60 mm in 48,000 steps. For
    example to dispense 9 mL from a 10 mL syringe you would determine the number of
    steps by multiplying 48000 steps (9 mL/10 mL) to get 43,200 steps.
    """

# TODO: 1) get switch by degrees, this should be simply set_position. Todo get command mapping as enum

    DEFAULT_CONFIG = {
        "default_infuse_rate": "1 ml/min",
        "default_withdraw_rate": "1 ml/min",
    }

    # This class variable is used for daisy chains (i.e. multiple pumps on the same serial connection). Details below.
    _io_instances: set[HamiltonPumpIO] = set()
    # The mutable object (a set) as class variable creates a shared state across all the instances.
    # When several pumps are daisy-chained on the same serial port, they need to all access the same Serial object,
    # because access to the serial port is exclusive by definition (also locking there ensure thread safe operations).

    # Only Hamilton syringes are compatible w/ the ML600, and they come on a limited set of sizes. (Values in ml)
    VALID_SYRINGE_VOLUME = {
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        25.0,
        50.0,
    }

    def __init__(
        self,
        pump_io: HamiltonPumpIO,
        syringe_volume: str,
        name: str,
        address: int = 1,
        **config,
    ) -> None:
        """Default constructor, needs an HamiltonPumpIO object. See from_config() class method for config-based init.

        Args:
        ----
            pump_io: An HamiltonPumpIO w/ serial connection to the daisy chain w/ target pump.
            syringe_volume: Volume of the syringe used, either a Quantity or number in ml.
            address: number of pump in array, 1 for first one, auto-assigned on init based on position.
            name: 'cause naming stuff is important.
        """
        super().__init__(name)
        self.device_info = DeviceInfo(
            authors=[dario, jakob, wei_hsin],
            manufacturer="Hamilton",
            model="ML600",
        )
        # HamiltonPumpIO
        self.pump_io = pump_io
        ML600._io_instances.add(self.pump_io)  # See above for details.

        # Pump address is the pump sequence number if in chain. Count starts at 1, default.
        self.address = int(address)

        # Syringe pumps only perform linear movement, and the volume displaced is function of the syringe loaded.
        try:
            self.syringe_volume = ureg.Quantity(syringe_volume)
        except AttributeError as attribute_error:
            logger.error(f"Invalid syringe volume {syringe_volume}!")
            raise InvalidConfigurationError(
                "Invalid syringe volume provided."
                "The syringe volume is a string with units! e.g. '5 ml'"
            ) from attribute_error

        if self.syringe_volume.m_as("ml") not in ML600.VALID_SYRINGE_VOLUME:
            raise InvalidConfigurationError(
                f"The specified syringe volume ({syringe_volume}) is invalid!\n"
                f"The volume (in ml) has to be one of {ML600.VALID_SYRINGE_VOLUME}"
            )

        self._steps_per_ml = ureg.Quantity(f"{48000 / self.syringe_volume} step/ml")
        self._max_vol = 48000 * ureg.step / self._steps_per_ml
        
        # This enables to configure on per-pump basis uncommon parameters
        self.config = ML600.DEFAULT_CONFIG | config
        self.dual_syringe = False

    @classmethod
    def from_config(cls, **config):
        """Create instances via config file."""
        # Many pump can be present on the same serial port with different addresses.
        # This shared list of HamiltonPumpIO objects allow shared state in a borg-inspired way, avoiding singletons
        # This is only relevant to programmatic instantiation, i.e. when from_config() is called per each pump from a
        # config file, as it is the case in the HTTP server.
        pumpio = None
        for obj in ML600._io_instances:
            # noinspection PyProtectedMember
            if obj._serial.port == config.get("port"):
                pumpio = obj
                break

        # If not existing serial object are available for the port provided, create a new one
        if pumpio is None:
            # Remove ML600-specific keys to only have HamiltonPumpIO's kwargs
            config_for_pumpio = {
                k: v
                for k, v in config.items()
                if k not in ("syringe_volume", "address", "name")
            }
            pumpio = HamiltonPumpIO.from_config(config_for_pumpio)

        return cls(
            pumpio,
            syringe_volume=config.get("syringe_volume", ""),
            address=config.get("address", 1),
            name=config.get("name", ""),
        )

    async def get_return_steps(self) -> int:
        """ Gives the dfined return steps for syringe movement """
        reply=await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.GET_RETURN_STEPS.value))
        return int(reply)

    async def set_return_steps(self, return_steps: int):
        # waiting is necessary since this happens on (class) initialisation
        target_steps = str(int(return_steps))
        await self.wait_until_idle()
        await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.SET_RETURN_STEPS.value, command_value=target_steps))

    async def initialize(self, hw_init=False, init_speed: str = "200 sec / stroke"):
        """Initialize pump and its components."""
        # this command MUST be executed in the beginning
        await self.pump_io.initialize()

        await self.set_return_steps(24)  # Steps added to each absolute move command, to decrease wear and tear at volume = 0, 24 is manual default
        # Test connectivity by querying the pump's firmware version
        self.device_info.version = await self.version()
        logger.info(
            f"Connected to Hamilton ML600 {self.name} - FW version: {self.device_info.version}!",
        )

        if hw_init:
            await self.initialize_pump(speed=ureg.Quantity(init_speed))

        #determine if syringe is single or dual - if dual, commands need to specify to which side theyre dispatched if
        # syringe/valve specific
        self.dual_syringe = not await self.single_syringe()

        # Add device components
        if self.dual_syringe:
            self.components.extend([ML600Pump("left_pump", self),ML600Pump("right_pump", self),
                                    ML600LeftValve("left_valve", self), ML600RightValve("right_valve", self)])
        else:
            self.components.extend([ML600Pump("pump", self), ML600LeftValve("valve", self)])
# TODO potentially set the suitable device mode - this might be needed despite switching by angle to achive the proper position reproducibly...


    async def send_command_and_read_reply(self, command: Protocol1Command) -> str:
        """Send a command to the pump. Here we just add the right pump number."""
        command.target_pump_num = self.address
        return await self.pump_io.write_and_read_reply_async(command)

    def _validate_speed(self, speed: pint.Quantity | None) -> str:
        """Validate the speed.

        Given a speed (seconds/stroke) returns a valid value for it, and a warning if out of bounds.
        """
        # Validated speeds are used as command argument, with empty string being the default for None
        if speed is None:
            return ""

        # Alert if out of bounds but don't raise exceptions, according to general philosophy.
        # Target flow rate too high
        if speed < ureg.Quantity("2 sec/stroke"):
            speed = ureg.Quantity("2 sec/stroke")
            warnings.warn(
                f"Desired speed ({speed}) is unachievable!"
                f"Set to {self._seconds_per_stroke_to_flowrate(speed)}"
                f"Wrong units? A bigger syringe is needed?",
                stacklevel=2,
            )

        # Target flow rate too low
        if speed > ureg.Quantity("3692 sec/stroke"):
            speed = ureg.Quantity("3692 sec/stroke")
            warnings.warn(
                f"Desired speed ({speed}) is unachievable!"
                f"Set to {self._seconds_per_stroke_to_flowrate(speed)}"
                f"Wrong units? A smaller syringe is needed?",
                stacklevel=2,
            )

        return str(round(speed.m_as("sec / stroke")))

    async def initialize_pump(self, speed: pint.Quantity | None = None):
        """Initialize both syringe and valve.

        speed: 2-3692 in seconds/stroke
        """
        init_pump = Protocol1Command(
            command=ML600Commands.INIT_ALL.value,
            optional_parameter=ML600Commands.OPTIONAL_PARAMETER.value,
            parameter_value=self._validate_speed(speed),
        )
        return await self.send_command_and_read_reply(init_pump)

    # async def initialize_valve(self):
    #     """Initialize valve only."""
    #     return await self.send_command_and_read_reply(Protocol1Command(command="LX"))

    # async def initialize_syringe(self, speed: pint.Quantity | None = None):
    #     """
    #     Initialize syringe only.
    #
    #     speed: 2-3692 in seconds/stroke
    #     """
    #     init_syringe = Protocol1Command(
    #         command="X1",
    #         optional_parameter="S",
    #         parameter_value=self._validate_speed(speed),
    #     )
    #     return await self.send_command_and_read_reply(init_syringe)

    def flowrate_to_seconds_per_stroke(self, flowrate: pint.Quantity):
        """Convert flow rates to steps per seconds.

        To determine the volume dispensed per step the total syringe volume is divided by
        48,000 steps. All Hamilton instrument syringes are designed with a 60 mm stroke
        length and the Microlab 600 is designed to move 60 mm in 48,000 steps. For
        example to dispense 9 mL from a 10 mL syringe you would determine the number of
        steps by multiplying 48000 steps (9 mL/10 mL) to get 43,200 steps.
        """
        flowrate_in_steps_sec = flowrate * self._steps_per_ml
        return (1 / flowrate_in_steps_sec).to("second/stroke")

    def _seconds_per_stroke_to_flowrate(self, second_per_stroke) -> float:
        """Convert seconds per stroke to flow rate."""
        flowrate = 1 / (second_per_stroke * self._steps_per_ml)
        return flowrate.to("ml/min")

    def _volume_to_step_position(self, volume: pint.Quantity) -> int:
        """Convert a volume to a step position."""
        # noinspection PyArgumentEqualDefault
        steps = volume * self._steps_per_ml
        return round(steps.m_as("steps"))

    async def _to_step_position(
        self,
        position: int,
        speed: pint.Quantity | None = None,
        syringe: ML600Commands = None
    ):
        """Absolute move to step position. If syringe side is required, give left or right"""
        abs_move_cmd = Protocol1Command(
            command=ML600Commands.ABSOLUTE_MOVE.value,
            optional_parameter=ML600Commands.OPTIONAL_PARAMETER.value,
            command_value=str(position),
            parameter_value=self._validate_speed(speed),
            target_component = syringe.value if self.dual_syringe else ""
        )
        return await self.send_command_and_read_reply(abs_move_cmd)

    async def get_current_volume(self, syringe: ML600Commands = None) -> pint.Quantity:
        """Return current syringe position in ml."""
        syringe_pos = await self.send_command_and_read_reply(
            Protocol1Command(command=ML600Commands.CURRENT_SYRINGE_POSITION.value,
                             target_component= syringe.value if self.dual_syringe else ""
),
        )

        current_steps = int(syringe_pos) * ureg.step
        return current_steps / self._steps_per_ml

    async def to_volume(self, target_volume: pint.Quantity, rate: pint.Quantity, syringe: ML600Commands = None):
        """Absolute move to volume provided."""
        speed = self.flowrate_to_seconds_per_stroke(rate)
        await self._to_step_position(
            self._volume_to_step_position(target_volume),
            speed,
            syringe=syringe
        )
        logger.debug(f"Pump {self.name} set to volume {target_volume} at speed {speed}")


    # TODO prob those also allow for selection
    async def pause(self):
        """Pause any running command."""
        return await self.send_command_and_read_reply(
            Protocol1Command(command="", execution_command=ML600Commands.PAUSE.value),
        )

    async def single_syringe(self)->bool:
        """Determine if single or dual syringe"""
        await self.wait_until_idle()
        is_single = await self.send_command_and_read_reply(
            Protocol1Command(command=ML600Commands.IS_SINGLE_SYRINGE.value),
        )
        if is_single == "N":
            return False
        elif is_single == "Y":
            return True
        else:
            raise InvalidConfigurationError("Neither single nor dual syringe - somethings wrong")


    async def resume(self):
        """Resume any paused command."""
        return await self.send_command_and_read_reply(
            Protocol1Command(command="", execution_command=ML600Commands.RESUME.value),
        )

    async def stop(self):
        """Stop and abort any running command."""
        await self.pause()
        return await self.send_command_and_read_reply(
            Protocol1Command(command="", execution_command=ML600Commands.CLEAR_BUFFER.value),
        )

    async def wait_until_idle(self):
        """Return when no more commands are present in the pump buffer."""
        logger.debug(f"ML600 pump {self.name} wait until idle...")
        while not await self.is_idle():
            await asyncio.sleep(0.1)
        logger.debug(f"...ML600 pump {self.name} idle now!")

    async def version(self) -> str:
        """Return the current firmware version reported by the pump."""
        return await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.FIRMWARE_VERSION.value))

    async def is_idle(self) -> bool:
        """Check if the pump is idle (actually check if the last command has ended)."""
        return (
            await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.REQUEST_DONE.value)) == "Y"
        )

    async def get_valve_position_by_name(self, valve: ML600Commands = None
) -> str:
        """
        Represent the position of the valve: getter returns Enum, setter needs Enum.
        Strongly encouraged to use switching by angle
        """
        return await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.CURRENT_VALVE_POSITION.value,
                                                                       target_component = valve.value if self.dual_syringe else ""))


    async def set_valve_position_by_name(
        self,
        target_position: str,
        wait_for_movement_end: bool = True,
            valve: ML600Commands = None
    ):
        """Set valve position.
        Strongly encouraged to use switching by angle
        wait_for_movement_end is defaulted to True as it is a common mistake not to wait...
        """
        await self.send_command_and_read_reply(

            Protocol1Command(command=ML600Commands.VALVE_BY_NAME_CW.value, command_value=target_position,
                             target_component = valve.value if self.dual_syringe else ""
),
        )
        logger.debug(f"{self.name} valve position set to position {target_position}")
        if wait_for_movement_end:
            await self.wait_until_idle()

    async def get_raw_position(self, target_component:str) -> str:
        """
        Represent the position of the valve: getter returns Enum, setter needs Enum.
        Strongly encouraged to use switching by angle
        """
        return await self.send_command_and_read_reply(Protocol1Command(command=ML600Commands.VALVE_ANGLE.value,
                                                                       target_component = target_component if self.dual_syringe else ""))
    async def set_raw_position(
        self,
        target_position: str,
        wait_for_movement_end: bool = True,
        counter_clockwise = False, 
        target_component = None
    ):
        """Set valve position.
        Strongly encouraged to use switching by angle
        wait_for_movement_end is defaulted to True as it is a common mistake not to wait...
        """
        if not counter_clockwise:
            await self.send_command_and_read_reply(
                Protocol1Command(command=ML600Commands.VALVE_BY_ANGLE_CW.value, command_value=target_position,
                                 target_component = target_component if self.dual_syringe else ""),
            )
            logger.debug(f"{self.name} valve position set to position {target_position}, switching CW")
        else:
            await self.send_command_and_read_reply(
                Protocol1Command(command=ML600Commands.VALVE_BY_ANGLE_CCW.value, command_value=target_position,
                                 target_component = target_component if self.dual_syringe else ""),
            )
            logger.debug(f"{self.name} valve position set to position {target_position}, switching CCW")
        if wait_for_movement_end:
            await self.wait_until_idle()


if __name__ == "__main__":
    import asyncio

    conf = {
        "port": "COM12",
        "address": 1,
        "name": "test1",
        "syringe_volume": "5 mL",
    }
    pump1 = ML600.from_config(**conf)
    asyncio.run(pump1.initialize_pump())
