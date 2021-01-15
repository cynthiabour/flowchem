from __future__ import annotations

import logging
import threading
import warnings
from pint import UnitRegistry
from enum import Enum
from typing import Union, List, Optional, Tuple
from dataclasses import dataclass
from time import sleep
import serial


class Elite11Exception(Exception):
    """ General pump exception """
    pass


class InvalidConfiguration(Elite11Exception):
    pass


class NotConnected(Elite11Exception):
    pass


class InvalidReply(Elite11Exception):
    pass


class InvalidCommand(Elite11Exception):
    pass


class InvalidArgument(Elite11Exception):
    pass


class UnachievableMove(Elite11Exception):
    pass


@dataclass
class Protocol11CommandTemplate:
    """ Class representing a pump command and its expected reply, but without target pump number """
    command_string: str
    reply_lines: int  # Reply line without considering leading newline and tailing prompt!
    requires_argument: bool

    def to_pump(self, address: int, argument: str = '') -> Protocol11Command:
        if self.requires_argument and not argument:
            raise InvalidArgument(f"Cannot send command {self.command_string} without an argument!")
        return Protocol11Command(command_string=self.command_string, reply_lines=self.reply_lines,
                                 requires_argument=self.requires_argument, target_pump_address=address,
                                 command_argument=argument)


@dataclass
class Protocol11Command(Protocol11CommandTemplate):
    """ Class representing a pump command and its expected reply """
    target_pump_address: int
    command_argument: str

    def compile(self, fast: bool = False) -> str:
        """
        Create actual command byte by prepending pump address to command.
        Fast saves some ms but do not update the display.
        """
        assert 0 <= self.target_pump_address < 99
        # end character needs to be '\r\n'. Since this command building is specific for elite 11, that should be fine
        if fast:
            return str(self.target_pump_address) + "@" + self.command_string + ' ' + self.command_argument + "\r\n"
        else:
            return str(self.target_pump_address) + self.command_string + ' ' + self.command_argument + "\r\n"


class PumpStatus(Enum):
    IDLE = ":"
    INFUSING = ">"
    WITHDRAWING = "<"
    TARGET_REACHED = "T"
    STALLED = "*"


class PumpIO:
    """ Setup with serial parameters, low level IO"""

    def __init__(self, port: Union[int, str], baud_rate: int = 115200):
        if baud_rate not in serial.serialutil.SerialBase.BAUDRATES:
            raise InvalidConfiguration(f"Invalid baud rate provided {baud_rate}!")

        if isinstance(port, int):
            port = f"COM{port}"  # Because I am lazy

        self.logger = logging.getLogger(__name__).getChild(self.__class__.__name__)
        self.lock = threading.Lock()

        try:
            self._serial = serial.Serial(port=port, baudrate=baud_rate, bytesize=serial.EIGHTBITS,
                                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.1,
                                         xonxoff=False, rtscts=False, write_timeout=None, dsrdtr=False,
                                         exclusive=True)  # type: Union[serial.serialposix.Serial, serial.serialwin32.Serial]
        except serial.serialutil.SerialException as e:
            raise InvalidConfiguration from e

    def _write(self, command: Protocol11Command):
        """ Writes a command to the pump """
        command = command.compile(fast=False)
        self.logger.debug(f"Sending {repr(command)}")
        try:
            self._serial.write(command.encode("ascii"))
        except serial.serialutil.SerialException as e:
            raise NotConnected from e

    def _read_reply(self, command) -> List[str]:
        """ Reads the pump reply from serial communication """
        reply_string = []
        self.logger.debug(f"I am going to read {command.reply_lines} line for this command (+prompt)")

        for _ in range(command.reply_lines + 2):  # +1 to account for leading newline character in reply + 1 prompt
            chunk = self._serial.readline().decode("ascii")
            self.logger.debug(f"Read line: {repr(chunk)} ")

            # Stripping newlines etc allows to skip empty lines and clean output
            chunk = chunk.strip()
            if chunk:
                reply_string.append(chunk)

        self.logger.debug(f"Reply received: {reply_string}")
        return reply_string

    @staticmethod
    def parse_response_line(line: str) -> Tuple[int, PumpStatus, str]:
        assert len(line) >= 3
        pump_address = int(line[0:2])
        status = PumpStatus(line[2:3])

        # Target reached is the only two-character status
        if status is PumpStatus.TARGET_REACHED:
            return pump_address, status, line[4:]
        else:
            return pump_address, status, line[3:]

    @staticmethod
    def parse_response(response: List[str], command: Protocol11Command) -> Union[str, List[str]]:
        # From every response line extract address, prompt and body
        parsed_lines = list(map(PumpIO.parse_response_line, response))
        pump_address, return_status, response = zip(*parsed_lines)

        # Check that all the replies came from the right pump
        assert all(address == command.target_pump_address for address in pump_address)

        # Ensure no stall is present
        if PumpStatus.STALLED in return_status:
            raise Elite11Exception("Pump stalled!")

        # Further response parsing
        if "Command error" in response[-1]:
            raise InvalidCommand(f"The command {command} is invalid for pump {command.target_pump_address}!"
                                 f"[Reply: {response}]")
        elif "Unknown command" in response[-1]:
            raise InvalidCommand(f"The command {command} is unknown to pump {command.target_pump_address}!"
                                 f"[Reply: {response}]")
        elif "Argument error" in response[-1]:
            raise InvalidArgument(f"The command {command} to pump {command.target_pump_address} includes an invalid"
                                  f" argument [Reply: {response}]")
        elif "Out of range" in response[-1]:
            raise InvalidArgument(f"The command {command} to pump {command.target_pump_address} has an argument "
                                  f"out of range! [Reply: {response}]")

        # If a single line is expected than return it directly as string, otherwise list of lines (empty list for 0)
        return response[0] if command.reply_lines == 1 else response

    def reset_buffer(self):
        try:
            self._serial.reset_input_buffer()
        except serial.PortNotOpenError as e:
            raise NotConnected from e

    def write_and_read_reply(self, command: Protocol11Command) -> List[str]:
        """  """
        with self.lock:
            self.reset_buffer()
            self._write(command)
            response = self._read_reply(command)

        return response

    @property
    def name(self) -> Optional[str]:
        try:
            return self._serial.name
        except AttributeError:
            return None


class Elite11Commands:

    """Holds the commands and arguments. Nota bene: Pump needs to be in Quick Start mode, which can be achieved from
     the display interface"""

    # collected commands
    # Methods can be programmed onto the pump and their execution remotely triggered.
    # No support is provided to such feature as "explicit is better than implicit", i.e. the same result can be obtained
    # with a sequence of Elite11Commands, with the advantage of ensuring code reproducibility (i.e. no specific
    # configuration is needed on the pump side)
    #
    # Other methods not included: dim display, usb echo, footswitch, poll, version (verbose ver), input,
    #                             output (if pin state high or low)

    EMPTY_MESSAGE = Protocol11CommandTemplate(command_string=" ", reply_lines=0, requires_argument=False)
    GET_VERSION = Protocol11CommandTemplate(command_string="VER", reply_lines=1, requires_argument=False)

    # RUN commands (no parameters, start movement in same direction/reverse direction/infuse/withdraw respectively)
    RUN = Protocol11CommandTemplate(command_string='run', reply_lines=0, requires_argument=False)
    REVERSE_RUN = Protocol11CommandTemplate(command_string='rrun', reply_lines=0, requires_argument=False)
    INFUSE = Protocol11CommandTemplate(command_string='irun', reply_lines=0, requires_argument=False)
    WITHDRAW = Protocol11CommandTemplate(command_string="wrun", reply_lines=0, requires_argument=False)

    # STOP movement
    STOP = Protocol11CommandTemplate(command_string='stp', reply_lines=0, requires_argument=False)

    # FORCE Pump force getter and setter, see Elite11.force property for range and suggested values
    GET_FORCE = Protocol11CommandTemplate(command_string="FORCE", reply_lines=1, requires_argument=False)
    SET_FORCE = Protocol11CommandTemplate(command_string="FORCE", reply_lines=1, requires_argument=True)

    # DIAMETER Syringe diameter getter and setter, see Elite11.diameter property for range and suggested values
    SET_DIAMETER = Protocol11CommandTemplate(command_string="diameter", reply_lines=1, requires_argument=False)
    GET_DIAMETER = Protocol11CommandTemplate(command_string="diameter", reply_lines=1, requires_argument=True)

    METRICS = Protocol11CommandTemplate(command_string="metrics", reply_lines=20, requires_argument=False)
    CURRENT_MOVING_RATE = Protocol11CommandTemplate(command_string="crate", reply_lines=1, requires_argument=False)

    # RAMP Ramping commands (infuse or withdraw)
    # setter: iramp [{start rate} {start units} {end rate} {end units} {ramp time in seconds}]
    GET_INFUSE_RAMP = Protocol11CommandTemplate(command_string="iramp", reply_lines=1, requires_argument=False)
    SET_INFUSE_RAMP = Protocol11CommandTemplate(command_string="iramp", reply_lines=1, requires_argument=True)
    GET_WITHDRAW_RAMP = Protocol11CommandTemplate(command_string="wramp", reply_lines=1, requires_argument=False)
    SET_WITHDRAW_RAMP = Protocol11CommandTemplate(command_string="wramp", reply_lines=1, requires_argument=True)

    # RATE
    # returns or set rate irate [max | min | lim | {rate} {rate units}]
    GET_INFUSE_RATE = Protocol11CommandTemplate(command_string="irate", reply_lines=1, requires_argument=False)
    GET_INFUSE_RATE_LIMITS = Protocol11CommandTemplate(command_string="irate lim", reply_lines=1, requires_argument=False)
    SET_INFUSE_RATE = Protocol11CommandTemplate(command_string="irate", reply_lines=1, requires_argument=True)
    GET_WITHDRAW_RATE = Protocol11CommandTemplate(command_string="wrate", reply_lines=1, requires_argument=False)
    GET_WITHDRAW_RATE_LIMITS = Protocol11CommandTemplate(command_string="wrate lim", reply_lines=1,
                                                         requires_argument=False)
    SET_WITHDRAW_RATE = Protocol11CommandTemplate(command_string="wrate", reply_lines=1, requires_argument=True)

    # GET VOLUME
    INFUSED_VOLUME = Protocol11CommandTemplate(command_string="ivolume", reply_lines=1, requires_argument=False)
    GET_SYRINGE_VOLUME = Protocol11CommandTemplate(command_string="svolume", reply_lines=1, requires_argument=False)
    SET_SYRINGE_VOLUME = Protocol11CommandTemplate(command_string="svolume", reply_lines=1, requires_argument=True)
    WITHDRAWN_VOLUME = Protocol11CommandTemplate(command_string="wvolume", reply_lines=1, requires_argument=False)

    # TARGET VOLUME
    GET_TARGET_VOLUME = Protocol11CommandTemplate(command_string="tvolume", reply_lines=1, requires_argument=False)
    SET_TARGET_VOLUME = Protocol11CommandTemplate(command_string="tvolume", reply_lines=1, requires_argument=True)

    # CLEAR VOLUME
    CLEAR_INFUSED_VOLUME = Protocol11CommandTemplate(command_string="civolume", reply_lines=0, requires_argument=False)
    CLEAR_WITHDRAWN_VOLUME = Protocol11CommandTemplate(command_string="cwvolume", reply_lines=0,
                                                       requires_argument=False)
    CLEAR_INFUSED_WITHDRAWN_VOLUME = Protocol11CommandTemplate(command_string="cvolume", reply_lines=0,
                                                               requires_argument=False)
    CLEAR_TARGET_VOLUME = Protocol11CommandTemplate(command_string="ctvolume", reply_lines=0, requires_argument=False)

    # GET TIME
    WITHDRAWN_TIME = Protocol11CommandTemplate(command_string="wtime", reply_lines=1, requires_argument=False)
    INFUSED_TIME = Protocol11CommandTemplate(command_string="itime", reply_lines=1, requires_argument=False)

    # TARGET TIME
    GET_TARGET_TIME = Protocol11CommandTemplate(command_string="ttime", reply_lines=1, requires_argument=False)
    SET_TARGET_TIME = Protocol11CommandTemplate(command_string="ttime", reply_lines=1, requires_argument=True)

    # CLEAR TIME
    CLEAR_INFUSED_TIME = Protocol11CommandTemplate(command_string="citime", reply_lines=0, requires_argument=False)
    CLEAR_INFUSED_WITHDRAW_TIME = Protocol11CommandTemplate(command_string="ctime", reply_lines=0,
                                                            requires_argument=False)
    CLEAR_TARGET_TIME = Protocol11CommandTemplate(command_string="cttime", reply_lines=0, requires_argument=False)
    CLEAR_WITHDRAW_TIME = Protocol11CommandTemplate(command_string="cwtime", reply_lines=0, requires_argument=False)


class Elite11:
    """
    Not ready for full usage.  Usable with: init with syringe diameter and volume. Set target volume and rate. run.
    """
    # Unit converter
    ureg = UnitRegistry()

    # first pump in chain/pump connected directly to computer, if pump chain connected MUST have address 0
    def __init__(self, pump_io: PumpIO, address: int = 0, name: str = None, diameter: float = None,
                 volume_syringe: float = None):
        """Query model and version number of firmware to check pump is
        OK. Responds with a load of stuff, but the last three characters
        are XXY, where XX is the address and Y is pump status. :, > or <
        when stopped, running forwards, or running backwards. Confirm
        that the address is correct. This acts as a check to see that
        the pump is connected and working."""

        self.pump_io = pump_io
        self.name = f"Pump {self.pump_io.name}:{address}" if name is None else name
        self.address: int = address
        if diameter is not None:
            self.diameter = diameter
        if volume_syringe is not None:
            self.syringe_volume = volume_syringe
        self.volume_syringe = volume_syringe

        self.log = logging.getLogger(__name__).getChild(__class__.__name__)

        # This command is used to test connection: failure handled by PumpIO
        self.log.info(f"Connected to pump '{self.name}' on port {self.pump_io.name}:{address} version: {self.version}!")
        # makes sure that a 'clean' pump is initialized.
        self.clear_times()
        self.clear_volumes()

        # Assume full syringe upon start-up
        self._volume_stored = self.syringe_volume

        # Can we raise an exception as soon as self._volume_stored becomes negative?
        self._target_volume = None

    def send_command_and_read_reply(self, command_template: Protocol11CommandTemplate, parameter='',
                                    parse=True) -> List[str]:
        """ Sends a command based on its template and return the corresponding reply """
        # Transforms the Protocol11CommandTemplate in the corresponding Protocol11Command by adding pump address
        pump_command = command_template.to_pump(self.address, parameter)
        response = self.pump_io.write_and_read_reply(pump_command)
        return PumpIO.parse_response(response, pump_command) if parse else response

    @staticmethod
    def bound(low, high, value: float, units: str) -> float:
        """ Bound the value provided to the interval [low - high] adn returns it with the same unit as provided."""
        value_w_units = Elite11.ureg.Quantity(value, units)
        return max(low, min(high, value_w_units)).m_as(units)

    @property
    def version(self) -> str:
        """ Returns the current firmware version reported by the pump """
        return self.send_command_and_read_reply(Elite11Commands.GET_VERSION)  # '11 ELITE I/W Single 3.0.4

    def get_status(self):
        """ Empty message to trigger a new reply and evaluate connection and pump current status via reply prompt """
        return PumpStatus(self.send_command_and_read_reply(Elite11Commands.EMPTY_MESSAGE, parse=False)[0][2:3])

    def is_moving(self) -> bool:
        """ Evaluate prompt for current status, i.e. moving or not """
        prompt = self.get_status()
        return prompt in (PumpStatus.INFUSING, PumpStatus.WITHDRAWING)

    @property
    def syringe_volume(self) -> float:
        """ Sets/returns the syringe volume in ml. """
        volume_w_units = self.send_command_and_read_reply(Elite11Commands.GET_SYRINGE_VOLUME)  # e.g. '100 ml'
        return Elite11.ureg(volume_w_units).m_as("ml")  # Unit registry does the unit conversion and returns ml

    @syringe_volume.setter
    def syringe_volume(self, volume_in_ml: float = None):
        self.send_command_and_read_reply(Elite11Commands.SET_SYRINGE_VOLUME, parameter=f"{volume_in_ml} m")

    def update_stored_volume(self):
        withdrawn = self.get_withdrawn_volume()
        infused = self.get_infused_volume()
        net_volume = withdrawn-infused
        # not really nice, also the target_volume and rate should be class attributes?
        self._volume_stored += net_volume
        # clear stored i w volume
        if withdrawn+infused != 0:
            self.clear_infused_withdrawn_volume()

    # TODO: when sending itime, pump will return the needed time for infusion of target volume. this could be used for time efficiency
    def run(self):
        # actually should be avoided, because in principle, this will move in any direction that it move before
        # TODO if stp while infuse/withdraw: get the infused withdrawn volume and correct
        """activates pump, runs in the previously set direction"""

        # this takes ANY volume changes before, updates internal variable and runs
        self.update_stored_volume()
        if self.is_moving():
            # should raise exception
            raise UnachievableMove("Pump already is moving")

        # if target volume is set, check if this is achievable
        elif self._target_volume is not None and self._volume_stored < self._target_volume:
            raise UnachievableMove("Pump contains less volume than required")
        else:
            self.send_command_and_read_reply(Elite11Commands.RUN)

        self.log.info("Pump started to run")

    def inverse_run(self):
        """activates pump, runs opposite to previously set direction"""
        self.send_command_and_read_reply(Elite11Commands.REVERSE_RUN)
        self.log.info("Pump started to run in reverse direction")

    def infuse_run(self):
        """activates pump, runs in infuse mode"""
        self.update_stored_volume()

        if self.is_moving():
            raise UnachievableMove("Pump already is moving")

        # if target volume is set, check if this is achievable
        elif self._target_volume:
            if self._volume_stored < self._target_volume:
                raise UnachievableMove("Pump contains less volume than required")
        else:
            self.send_command_and_read_reply(Elite11Commands.INFUSE)

        self.log.info("Pump started to infuse")

    def withdraw_run(self):
        """activates pump, runs in withdraw mode"""

        self.update_stored_volume()

        if self.is_moving():
            raise UnachievableMove("Pump already is moving")

        # if target volume is set, check if this is achievable
        elif self._target_volume:
            if self._volume_stored + self._target_volume > self.volume_syringe:
                raise UnachievableMove("Pump would be overfilled")
        else:
            self.send_command_and_read_reply(Elite11Commands.WITHDRAW)

        self.log.info("Pump started to withdraw")

    def stop(self):
        """stops pump"""
        self.send_command_and_read_reply(Elite11Commands.STOP)
        self.update_stored_volume()

        self.log.info("Pump stopped")

        # metrics, syringevolume

    @property
    def infusion_rate(self) -> float:
        """ Returns/set the infusion rate in ml*min-1 """
        rate_w_units = self.send_command_and_read_reply(Elite11Commands.GET_INFUSE_RATE)  # e.g. '09:0.2 ml/min'
        return Elite11.ureg(rate_w_units).m_as("ml/min")  # Unit registry does the unit conversion and returns ml/min

    @infusion_rate.setter
    def infusion_rate(self, rate_in_ml_min):
        # Get current pump limits (those are function of the syringe diameter)
        limits_raw = self.send_command_and_read_reply(Elite11Commands.GET_INFUSE_RATE_LIMITS)
        lower_limit, upper_limit = map(Elite11.ureg, limits_raw.split(" to "))

        # Bound the provided rate to the limits
        set_rate = Elite11.bound(low=lower_limit, high=upper_limit, value=rate_in_ml_min, units="ml/min")

        # If the set rate was adjusted to fit limits warn user
        if set_rate != rate_in_ml_min:
            warnings.warn(f"The requested rate {rate_in_ml_min} ml/min was outside the acceptance range"
                          f"[{lower_limit} - {upper_limit}] and was bounded to {set_rate} ml/min!")

        # Finally set the rate
        self.send_command_and_read_reply(Elite11Commands.SET_INFUSE_RATE, parameter=f"{set_rate} m/m")

    @property
    def withdrawing_rate(self) -> float:
        """ Returns/set the infusion rate in ml*min-1 """
        rate_w_units = self.send_command_and_read_reply(Elite11Commands.GET_WITHDRAW_RATE)
        return Elite11.ureg(rate_w_units).m_as("ml/min")  # Unit registry does the unit conversion and returns ml/min

    @withdrawing_rate.setter
    def withdrawing_rate(self, rate_in_ml_min):
        # Get current pump limits (those are function of the syringe diameter)
        limits_raw = self.send_command_and_read_reply(Elite11Commands.GET_WITHDRAW_RATE_LIMITS)  # e.g. '116.487 nl/min to 120.967 ml/min'
        lower_limit, upper_limit = map(Elite11.ureg, limits_raw.split(" to "))

        # Bound the provided rate to the limits
        set_rate = Elite11.bound(low=lower_limit, high=upper_limit, value=rate_in_ml_min, units="ml/min")

        # If the set rate was adjusted to fit limits warn user
        if set_rate != rate_in_ml_min:
            warnings.warn(f"The requested rate {rate_in_ml_min} ml/min was outside the acceptance range"
                          f"[{lower_limit} - {upper_limit}] and was bounded to {set_rate} ml/min!")

        # Finally set the rate
        self.send_command_and_read_reply(Elite11Commands.SET_WITHDRAW_RATE, parameter=f"{set_rate} m/m")

    def get_infused_volume(self) -> float:
        """ Return infused volume in ml """
        return Elite11.ureg(self.send_command_and_read_reply(Elite11Commands.INFUSED_VOLUME)).m_as("ml")

    def get_withdrawn_volume(self):
        return Elite11.ureg(self.send_command_and_read_reply(Elite11Commands.WITHDRAWN_VOLUME)).m_as("ml")

    def clear_infused_volume(self):
        self.send_command_and_read_reply(Elite11Commands.CLEAR_INFUSED_VOLUME)

    def clear_withdrawn_volume(self):
        self.send_command_and_read_reply(Elite11Commands.CLEAR_WITHDRAWN_VOLUME)

    def clear_infused_withdrawn_volume(self):
        self.send_command_and_read_reply(Elite11Commands.CLEAR_INFUSED_WITHDRAWN_VOLUME)
        sleep(0.1)

    @property
    def infuse_ramp(self):
        raw_ramp = self.send_command_and_read_reply(Elite11Commands.GET_INFUSE_RAMP)
        if raw_ramp == "Ramp not set up.":
            return None
        else:
            raise NotImplementedError

    @infuse_ramp.setter
    def infuse_ramp(self, rate):
        raise NotImplementedError

    @property
    def withdraw_ramp(self):
        raw_ramp = self.send_command_and_read_reply(Elite11Commands.GET_WITHDRAW_RAMP)
        if raw_ramp == "Ramp not set up.":
            return None
        else:
            raise NotImplementedError

    @withdraw_ramp.setter
    def withdraw_ramp(self, rate):
        raise NotImplementedError

    @property
    def force(self):
        """
        Pump force, in percentage.
        Manufacturer suggested values are:
            stainless steel:    100%
            plastic syringes:   50% if volume <= 5 ml else 100%
            glass/glass:        30% if volume <= 20 ml else 50%
            glass/plastic:      30% if volume <= 250 ul, 50% if volume <= 5ml else 100%
        """
        return int(self.send_command_and_read_reply(Elite11Commands.GET_FORCE)[0][3:-1])

    @force.setter
    def force(self, force_percent: int):
        self.send_command_and_read_reply(Elite11Commands.SET_FORCE, parameter=str(force_percent))

    @property
    def diameter(self) -> float:
        """
        Syringe diameter in mm. This can be set in the interval 1 mm to 33 mm
        """
        return float(self.send_command_and_read_reply(Elite11Commands.SET_DIAMETER)[:-3])  # "31.1232 mm" removes unit

    @diameter.setter
    def diameter(self, diameter_in_mm: float):
        if not 1 <= diameter_in_mm <= 33:
            raise InvalidArgument(f"Diameter provided ({diameter_in_mm}) is not valid! [Accepted range: 1-33 mm]")

        self.send_command_and_read_reply(Elite11Commands.SET_DIAMETER, parameter=f"{diameter_in_mm:.4f}")

    def display_current_rate(self):
        """
        If pump moves, this returns the current moving rate. Else return is not sensible
        :return: current moving rate
        """
        return self.send_command_and_read_reply(Elite11Commands.CURRENT_MOVING_RATE)

    @property
    def target_volume(self) -> float:
        """
        Set/returns target volume in ml.
        """
        return float(self.send_command_and_read_reply(Elite11Commands.GET_TARGET_VOLUME))

    @target_volume.setter
    def target_volume(self, target_volume_in_ml: float):
        self.send_command_and_read_reply(Elite11Commands.SET_TARGET_VOLUME, parameter=f"{target_volume_in_ml} m")

    def target_time(self, target_time: str):
        return self.send_command_and_read_reply(Elite11Commands.TARGET_VOLUME, parameter=target_time)

    def clear_volumes(self):
        self.send_command_and_read_reply(Elite11Commands.CLEAR_TARGET_VOLUME)
        self.send_command_and_read_reply(Elite11Commands.CLEAR_INFUSED_WITHDRAWN_VOLUME)
        self._target_volume = None

    def clear_times(self):
        self.send_command_and_read_reply(Elite11Commands.CLEAR_INFUSED_WITHDRAW_TIME)
        self.send_command_and_read_reply(Elite11Commands.CLEAR_TARGET_TIME)


# TARGET VOLuME AND TIME ARE THE THINGS TO USE!!! Rate needs to be set, infuse or withdraw, then simply start!


"""
TODO:
    - T* should be included, and ensure that an object can be initialized from graph-provided info
    - if pump in isn't in quick start mode: reply is command error Nonsystem commnds bla bla so this is caught, maybe get more explanatory logging message
    - tests?
"""


if __name__ == '__main__':
    # from flowchem.devices.Harvard_Apparatus.HA_elite11 import *
    # import logging
    logging.basicConfig()
    logging.getLogger('flowchem').setLevel(logging.DEBUG)

    a = PumpIO(5)
    p = Elite11(a, 9)
