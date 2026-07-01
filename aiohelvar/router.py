from aiohelvar.parser.command_parameter import CommandParameterType
from .devices import Devices, get_devices
from .groups import Groups, get_groups
from .scenes import Scenes, get_scenes
from .parser.parser import CommandParser
from .parser.command_type import (
    COMMAND_TYPES_DONT_LISTEN_FOR_RESPONSE,
    CommandType,
    MessageType,
)
from .parser.command import Command
from .exceptions import CommandResponseTimeout, ParserError
import asyncio
import datetime
import logging
import ipaddress

_LOGGER = logging.getLogger(__name__)


COMMAND_TERMINATOR = b"#"

# Some commands take a long time to process, and if the router has a significant queue, we
# can be waiting some time. Setting this to a somewhat absurd 30 seconds.
COMMAND_RESPONSE_TIMEOUT = 30

KEEP_ALIVE_PERIOD = 120


class Router:
    """Control a Helvar Router."""

    def __init__(self, host, port, cluster_id=0, router_id=1, use_specified_ids=False):
        """Create a Router.

        When ``use_specified_ids`` is False (the default) the HelvarNet cluster
        and router ids are derived from an IPv4 host as cluster = 3rd octet,
        router = 4th octet. Per the Designer 5 Quick Start Guide (section 3.4,
        "Clusters: cluster masks and Router IDs") this is only correct for the
        Helvar default cluster mask 255.255.255.0 with the usual 10.254.C.R
        layout. For other cluster masks, or when the router is reached on an
        unrelated IP (e.g. via a bridge on a 192.168.x.y network), the derived
        ids will be wrong - pass ``cluster_id``/``router_id`` with
        ``use_specified_ids=True`` instead. (Note: the HelvarNet API/TCP port is
        50000; 60005 is the separate inter-router "cluster comms" port.)
        """
        self.host = host
        self.port = port

        # Check if we should use specified IDs or extract from IP address
        if use_specified_ids:
            # Use the provided cluster_id and router_id values
            _LOGGER.debug(f"Using specified IDs: cluster_id={cluster_id}, router_id={router_id}")
            self.cluster_id = cluster_id
            self.router_id = router_id
        else:
            # Check if host is a valid IP address and extract cluster_id and router_id
            try:
                ip = ipaddress.ip_address(host)
                if isinstance(ip, ipaddress.IPv4Address):
                    octets = str(ip).split('.')
                    self.cluster_id = int(octets[2])  # 3rd octet
                    self.router_id = int(octets[3])   # 4th octet
                    _LOGGER.debug(f"Extracted IDs from IPv4 address {host}: cluster_id={self.cluster_id}, router_id={self.router_id}")
                else:
                    # For IPv6 or if we can't parse octets, use provided values
                    _LOGGER.debug(f"IPv6 address {host} detected, using provided values: cluster_id={cluster_id}, router_id={router_id}")
                    self.cluster_id = cluster_id
                    self.router_id = router_id
            except ValueError:
                # Not a valid IP address, use provided values
                _LOGGER.debug(f"Invalid IP address '{host}', using provided values: cluster_id={cluster_id}, router_id={router_id}")
                self.cluster_id = cluster_id
                self.router_id = router_id

        self.config = None

        self.groups = Groups(self)

        self.devices = Devices(self)

        self.lights = None
        self.scenes = Scenes(self)
        self.sensors = None

        self.commands_to_send = asyncio.Queue()

        self.commands_received = []
        self.command_received = asyncio.Condition()

        self.connected = False

        # Connection state. Populated by open()/connect(); initialised here so
        # that disconnect() is safe to call even if we never connected.
        self._reader = None
        self._writer = None
        self._stream_reader_task = None
        self._stream_writer_task = None
        self._keep_alive_task = None

        self.workgroup_name = None

    @property
    def id(self):
        """Return the ID of the router."""
        if self.config is not None:
            return self.config.routerid

        return self._router_id

    async def open(self):
        """Open the TCP connection and start the stream reader/writer tasks.

        This performs no HelvarNet queries and starts no keepalive, so it cannot
        block waiting for a router reply. It is the low-level primitive shared by
        connect() and used by the read-only diagnostics, which need the command
        machinery running but want to bound every query with their own timeout.
        """
        _LOGGER.debug(f"Opening connection to {self.host}:{self.port}...")

        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port
            )
        except ConnectionError as e:
            _LOGGER.error(
                f"Connection error while connecting to router {self.host}:{self.port} - {e}"
            )
            raise
        self.connected = True
        self._stream_reader_task = asyncio.create_task(
            self._stream_reader(self._reader)
        )
        self._stream_writer_task = asyncio.create_task(
            self._stream_writer(self._reader, self._writer)
        )

    async def connect(self):
        _LOGGER.debug("Connecting...")

        await self.open()

        # Read the workgroup name:
        response = await self._send_command_task(
            Command(CommandType.QUERY_WORKGROUP_NAME)
        )
        self.workgroup_name = response.result

        # Kick off the keepalive task
        self._keep_alive_task = asyncio.create_task(self._keep_alive())

    async def reconnect(self):
        await self.disconnect()
        await self.connect()

    async def disconnect(self):
        _LOGGER.info("Disconnecting...")
        tasks = [
            self._stream_reader_task,
            self._stream_writer_task,
            self._keep_alive_task,
        ]

        for task in tasks:
            if task is not None:
                task.cancel()

        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
        self.connected = False
        _LOGGER.info("Disconnected.")

    async def _keep_alive(self):
        """Keep the TCP connection alive. This'll also clean up any stale command futures."""

        def _keep_alive_callback(task):

            if task.exception():
                _LOGGER.warn(
                    f"Keep alive encountered an exception: {task.exception()}."
                )
                if isinstance(task.exception(), CommandResponseTimeout):
                    # Timeout - reconnect.
                    _LOGGER.warn("Keepalive didn't - reconnecting...")
                    asyncio.create_task(self.reconnect())
                    return
                else:
                    raise (task.exception())
            _LOGGER.debug("Keepalive kept the router TCP connection alive.")

        while True:
            await asyncio.sleep(KEEP_ALIVE_PERIOD)
            keepalive = await self.send_command(Command(CommandType.QUERY_ROUTER_TIME))

            keepalive.add_done_callback(_keep_alive_callback)

    async def _stream_reader(self, reader):
        _LOGGER.info("Connected.")
        parser = CommandParser()

        while True:
            line = await reader.readuntil(COMMAND_TERMINATOR)
            if line is not None:

                _LOGGER.debug(f"Received line: {line}")

                lines = line.split(b"$")
                if len(lines) > 1:
                    _LOGGER.debug(f"Split line by '$' into {len(lines)} lines")

                for splitline in lines:
                    _LOGGER.debug(f"Parsing line: {splitline}")
                    try:
                        command = parser.parse_command(splitline)
                    except ParserError as e:
                        _LOGGER.error(f"Exception handling line from router: {e}")
                    except Exception as e:
                        raise e
                    else:
                        _LOGGER.info(f"Received command: {command}")

                        if command.command_type == CommandType.RECALL_SCENE:
                            asyncio.create_task(self.handle_scene_recall(command))
                            continue

                        await self.command_received.acquire()
                        self.commands_received.append(command)
                        self.command_received.notify_all()
                        self.command_received.release()

    async def _stream_writer(self, reader, writer):

        while True:
            command_string = await self.commands_to_send.get()
            _LOGGER.info(f"Sending command '{command_string}'...")
            writer.write(command_string)
            # Small buffer. It's possible to overload a router.
            await asyncio.sleep(0.01)
            await writer.drain()
            self.commands_to_send.task_done()

    async def wait_for_pending_replies(self):
        while True:
            if len(self.command_received._waiters) == 0:
                return
            await asyncio.sleep(0.1)

    async def initialize(self):

        # Attempt Connection
        if not self.connected:
            await self.connect()

        # Get Groups
        await self.get_groups()

        # Get Devices
        await self.get_devices()

        # Get Clusters
        # await self.get_clusters()

        # Get Scenes
        await self.get_scenes()

        # Update group scenes
        await self.groups.force_update_groups()

    async def get_groups(self):

        await get_groups(self)

    async def get_devices(self):

        await get_devices(self)

    async def get_scenes(self):

        await get_scenes(self, self.groups)

    # async def get_clusters(self):
    #     response = await self.send_command(Command(CommandType.QUERY_ROUTERS))

    #     await response

    #     print(response.result())

    async def _send_command_task(self, command: Command):

        start_time = datetime.datetime.now()

        await self.send_string(str(command))

        def check_for_command_response():
            """Task that is scheduled after every command is sent. It checks for incoming messages
            from the router, looking for its reply.
            We match all command parameters, but we can't guarantee that identical requests don't steal
            eachothers replies."""

            for r_command in self.commands_received:
                if r_command.type_parameters_address == command.type_parameters_address:
                    # this is probably our response.
                    # We can safely remove ourselves from list as we stop iterating.

                    if r_command.command_message_type == MessageType.ERROR:
                        _LOGGER.error(
                            f"Request command {command} triggered an error back from the router: {r_command}."
                        )

                    self.commands_received.remove(r_command)
                    return r_command
            return None

        if command.command_type in COMMAND_TYPES_DONT_LISTEN_FOR_RESPONSE:
            return None

        response = check_for_command_response()

        if response:
            return response

        async with self.command_received:
            while response is None:

                if datetime.datetime.now() > (
                    start_time + datetime.timedelta(0, COMMAND_RESPONSE_TIMEOUT)
                ):
                    raise CommandResponseTimeout(command)

                await self.command_received.wait()

                response = check_for_command_response()
                if response:
                    break

        return response

    async def send_command(self, command: Command) -> asyncio.Task:
        """
        Send command, return a future that'll return when we get a response back.
        We don't have request identifiers, so we have to use basic FIFO and
        assume the router executes commands in the order it received them.
        """
        return asyncio.create_task(self._send_command_task(command))

    async def query(self, command: Command, timeout: float = None):
        """Send a command and await its response, optionally bounded by a timeout.

        Unlike send_command(), which returns a task, this awaits the reply and
        returns the response Command. If ``timeout`` (seconds) is given and no
        reply arrives in time, asyncio.TimeoutError is raised instead of blocking
        for the full default COMMAND_RESPONSE_TIMEOUT. Read-only callers such as
        the diagnostics use this to probe a router without ever hanging.
        """
        if timeout is None:
            return await self._send_command_task(command)
        return await asyncio.wait_for(self._send_command_task(command), timeout)

    async def query_dali2_energy(self, address, timeout: float = None):
        """Query DALI-2 energy reporting (C:252) for a device. Returns a DALI2Result.

        DALI-2 feature for newer routers (e.g. 950). On routers that don't support
        it (905/910/920, old firmware) this raises DALI2NotSupportedError.
        """
        from .dali2 import query_energy

        return await query_energy(self, address, timeout)

    async def query_dali2_diagnostics(self, address, timeout: float = None):
        """Query DALI-2 diagnostics & maintenance (C:253). Returns a DALI2Result.

        DALI-2 feature for newer routers (e.g. 950). On routers that don't support
        it (905/910/920, old firmware) this raises DALI2NotSupportedError.
        """
        from .dali2 import query_diagnostics

        return await query_diagnostics(self, address, timeout)

    async def send_string(self, string: str):
        await self.commands_to_send.put(bytes(string, "utf-8"))

    async def handle_scene_recall(self, command: Command):
        """
        The only notifications we get on live changes in levels of devices is through scenes.
        """

        scene_address = command.get_scene_address()
        fade_time = command.get_param_value(CommandParameterType.FADE_TIME)

        await self.groups.handle_scene_callback(scene_address, fade_time)
