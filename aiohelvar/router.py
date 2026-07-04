from aiohelvar.parser.command_parameter import CommandParameterType
from .devices import Devices, get_devices
from .groups import Groups, get_groups
from .lib import parse_id_list
from .parser.address import HelvarAddress
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

# Bound for the cluster/router id discovery queries run right after connecting.
# Discovery must never hang a connection attempt for the full command timeout.
DISCOVERY_TIMEOUT = 10

# Routers are embedded devices that answer queries one at a time; flooding one
# with hundreds of concurrent queries (initialisation of a site with dozens of
# devices/groups) makes it fall behind until replies arrive after the timeout
# or not at all. Commands therefore share a small pool of in-flight slots.
MAX_CONCURRENT_COMMANDS = 4

# How long to wait between reconnect attempts after the connection drops.
RECONNECT_RETRY_DELAY = 10

# Unmatched (usually late) replies are pruned by the keepalive so they cannot
# accumulate forever. Far above the in-flight limit, so replies that are just
# slow still get matched.
MAX_UNMATCHED_REPLIES = 32


class Router:
    """Control a Helvar Router."""

    def __init__(self, host, port, cluster_id=0, router_id=1, use_specified_ids=False):
        """Create a Router.

        The HelvarNet cluster and router ids are discovered from the router
        itself right after connecting (QUERY_CLUSTERS C:101 + QUERY_ROUTERS
        C:102) - see :meth:`discover_router_ids`. This works regardless of how
        the router is addressed on the LAN.

        Before discovery has run (and as a last-resort fallback if both
        discovery queries fail) the ids are guessed from an IPv4 host as
        cluster = 3rd octet, router = 4th octet. Per the Designer 5 Quick Start
        Guide (section 3.4, "Clusters: cluster masks and Router IDs") that
        heuristic is only correct for the Helvar default cluster mask
        255.255.255.0 with the usual 10.254.C.R layout - on e.g. a 192.168.x.y
        network it probes a non-existent cluster (HelvarNet error 9), which is
        why runtime discovery is the default.

        Pass ``cluster_id``/``router_id`` with ``use_specified_ids=True`` to
        skip discovery and force specific ids. (Note: the HelvarNet API/TCP
        port is 50000; 60005 is the separate inter-router "cluster comms"
        port.)
        """
        self.host = host
        self.port = port

        self._use_specified_ids = use_specified_ids

        # Discovered topology; populated by discover_router_ids().
        self.clusters = []
        self.cluster_routers = {}
        self.ids_discovered = False
        self._discovery_attempted = False

        if use_specified_ids:
            # Use the provided cluster_id and router_id values
            _LOGGER.debug(f"Using specified IDs: cluster_id={cluster_id}, router_id={router_id}")
            self.cluster_id = cluster_id
            self.router_id = router_id
        else:
            # Initial guess until discovery runs: derive from an IPv4 host,
            # falling back to the provided defaults.
            self.cluster_id, self.router_id = self._ids_from_host(
                host, cluster_id, router_id
            )

        self.config = None

        self.groups = Groups(self)

        self.devices = Devices(self)

        self.lights = None
        self.scenes = Scenes(self)
        self.sensors = None

        self.commands_to_send = asyncio.Queue()

        self.commands_received = []
        self.command_received = asyncio.Condition()

        # Limit in-flight commands so router initialisation on large sites
        # doesn't flood the router - see MAX_CONCURRENT_COMMANDS.
        self._command_slots = asyncio.Semaphore(MAX_CONCURRENT_COMMANDS)

        self.connected = False

        # True while a deliberate disconnect is in progress/complete;
        # suppresses automatic reconnection.
        self._closing = False
        self._reconnect_task = None

        # Connection state. Populated by open()/connect(); initialised here so
        # that disconnect() is safe to call even if we never connected.
        self._reader = None
        self._writer = None
        self._stream_reader_task = None
        self._stream_writer_task = None
        self._keep_alive_task = None

        self.workgroup_name = None

    @staticmethod
    def _ids_from_host(host, default_cluster_id, default_router_id):
        """Guess (cluster_id, router_id) from an IPv4 host address.

        Only correct on the Helvar 10.254.C.R addressing convention; used as an
        initial value and as a last-resort fallback when runtime discovery
        fails.
        """
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            _LOGGER.debug(
                f"Host '{host}' is not an IP address, using provided values: "
                f"cluster_id={default_cluster_id}, router_id={default_router_id}"
            )
            return default_cluster_id, default_router_id

        if isinstance(ip, ipaddress.IPv4Address):
            octets = str(ip).split(".")
            cluster_id = int(octets[2])  # 3rd octet
            router_id = int(octets[3])  # 4th octet
            _LOGGER.debug(
                f"Guessed IDs from IPv4 address {host}: cluster_id={cluster_id}, router_id={router_id}"
            )
            return cluster_id, router_id

        _LOGGER.debug(
            f"IPv6 address {host} detected, using provided values: "
            f"cluster_id={default_cluster_id}, router_id={default_router_id}"
        )
        return default_cluster_id, default_router_id

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

        self._closing = False
        await self.open()

        # Read the workgroup name:
        response = await self._send_command_task(
            Command(CommandType.QUERY_WORKGROUP_NAME)
        )
        self.workgroup_name = response.result

        # Discover the real cluster/router ids from the router itself.
        await self.discover_router_ids()

        # Kick off the keepalive task
        self._keep_alive_task = asyncio.create_task(self._keep_alive())

    async def discover_router_ids(self):
        """Discover the cluster and router ids from the connected router.

        Sends QUERY_CLUSTERS (C:101) and then QUERY_ROUTERS (C:102) per
        cluster. Real firmware requires the cluster as the address parameter
        of C:102 (">V:2,C:102,@<cluster>#"); a bare C:102 is answered with
        error 17 ("Missing ASCII parameter").

        The first discovered cluster/router pair becomes this Router's
        cluster_id/router_id. The full topology is kept in ``self.clusters``
        and ``self.cluster_routers``. Skipped when the Router was created with
        ``use_specified_ids=True``. If both discovery queries fail, the
        IP-derived heuristic ids from __init__ are kept as a last resort.
        """
        self._discovery_attempted = True

        if self._use_specified_ids:
            _LOGGER.debug(
                "Skipping cluster/router discovery: using specified ids "
                f"cluster_id={self.cluster_id}, router_id={self.router_id}"
            )
            return

        clusters = await self._discover_clusters()
        cluster_routers = {}
        for cluster in clusters:
            routers = await self._discover_routers_in_cluster(cluster)
            if routers:
                cluster_routers[cluster] = routers

        self.clusters = clusters
        self.cluster_routers = cluster_routers

        for cluster in clusters:
            routers = cluster_routers.get(cluster)
            if routers:
                self.cluster_id = cluster
                self.router_id = routers[0]
                self.ids_discovered = True
                _LOGGER.info(
                    f"Discovered HelvarNet topology {cluster_routers}; using "
                    f"cluster_id={self.cluster_id}, router_id={self.router_id}"
                )
                return

        _LOGGER.warning(
            "Could not discover cluster/router ids from the router "
            f"(clusters={clusters}, routers={cluster_routers}). Falling back "
            f"to the IP-derived guess cluster_id={self.cluster_id}, "
            f"router_id={self.router_id}. If device discovery fails, pass "
            "cluster_id/router_id with use_specified_ids=True."
        )

    async def _discover_clusters(self):
        """Return the list of cluster ids the router reports (C:101)."""
        try:
            response = await self.query(
                Command(CommandType.QUERY_CLUSTERS), timeout=DISCOVERY_TIMEOUT
            )
        except (asyncio.TimeoutError, CommandResponseTimeout):
            _LOGGER.warning("QUERY_CLUSTERS (C:101) timed out.")
            return []

        if response is None or response.command_message_type == MessageType.ERROR:
            _LOGGER.warning(f"QUERY_CLUSTERS (C:101) failed: {response}")
            return []

        return parse_id_list(response.result)

    async def _discover_routers_in_cluster(self, cluster_id):
        """Return the list of router ids in a cluster (C:102, @cluster)."""
        try:
            response = await self.query(
                Command(
                    CommandType.QUERY_ROUTERS,
                    command_address=HelvarAddress(cluster_id),
                ),
                timeout=DISCOVERY_TIMEOUT,
            )
        except (asyncio.TimeoutError, CommandResponseTimeout):
            _LOGGER.warning(f"QUERY_ROUTERS (C:102) for cluster {cluster_id} timed out.")
            return []

        if response is None or response.command_message_type == MessageType.ERROR:
            _LOGGER.warning(
                f"QUERY_ROUTERS (C:102) for cluster {cluster_id} failed: {response}"
            )
            return []

        return parse_id_list(response.result)

    async def reconnect(self):
        await self.disconnect()
        await self.connect()

    async def disconnect(self):
        _LOGGER.info("Disconnecting...")
        self._closing = True

        # Stop a pending automatic reconnect (unless we *are* the reconnect
        # task, which calls disconnect() as part of reconnecting).
        if (
            self._reconnect_task is not None
            and not self._reconnect_task.done()
            and self._reconnect_task is not asyncio.current_task()
        ):
            self._reconnect_task.cancel()

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
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                # The connection may already be gone - that's why we're here.
                pass
        self.connected = False
        _LOGGER.info("Disconnected.")

    def _start_reconnect(self):
        """Schedule an automatic reconnect after the connection was lost."""
        if self._closing:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self.connected = False
        _LOGGER.warning("Connection to the router was lost. Reconnecting...")
        self._reconnect_task = asyncio.create_task(self._reconnect_with_retries())

    async def _reconnect_with_retries(self):
        """Reconnect, retrying every RECONNECT_RETRY_DELAY seconds.

        Runs until the connection is back or the router is deliberately
        disconnected. Consumers (e.g. Home Assistant entities) can use
        ``self.connected`` to report availability in the meantime.
        """
        while not self._closing:
            try:
                await self.reconnect()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning(
                    f"Reconnect to {self.host}:{self.port} failed ({err!r}). "
                    f"Retrying in {RECONNECT_RETRY_DELAY}s..."
                )
                await asyncio.sleep(RECONNECT_RETRY_DELAY)
            else:
                _LOGGER.info("Reconnected to the router.")
                return

    async def _keep_alive(self):
        """Keep the TCP connection alive and prune stale unmatched replies."""

        def _keep_alive_callback(task):

            if task.cancelled():
                return
            if task.exception():
                _LOGGER.warning(
                    f"Keep alive encountered an exception: {task.exception()!r}."
                )
                if isinstance(task.exception(), CommandResponseTimeout):
                    # Timeout - reconnect (with retries).
                    _LOGGER.warning("Keepalive didn't - reconnecting...")
                    self._start_reconnect()
                return
            _LOGGER.debug("Keepalive kept the router TCP connection alive.")

        while True:
            await asyncio.sleep(KEEP_ALIVE_PERIOD)

            # Replies that no waiter ever matched (usually replies that
            # arrived after their query timed out) must not pile up forever.
            stale = len(self.commands_received) - MAX_UNMATCHED_REPLIES
            if stale > 0:
                _LOGGER.debug(f"Pruning {stale} stale unmatched replies.")
                del self.commands_received[:stale]

            keepalive = await self.send_command(Command(CommandType.QUERY_ROUTER_TIME))

            keepalive.add_done_callback(_keep_alive_callback)

    async def _stream_reader(self, reader):
        _LOGGER.info("Connected.")
        parser = CommandParser()

        while True:
            try:
                line = await reader.readuntil(COMMAND_TERMINATOR)
            except asyncio.CancelledError:
                raise
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as err:
                # Connection dropped (router reboot, network blip). Trigger an
                # automatic reconnect instead of dying silently - a dead
                # reader used to leave every future query timing out.
                _LOGGER.warning(f"Reading from the router failed: {err!r}")
                self._start_reconnect()
                return

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
                    except Exception as e:  # pylint: disable=broad-except
                        # One malformed line must never kill the reader task -
                        # that would silently stop all response processing.
                        _LOGGER.error(
                            f"Unexpected error parsing line {splitline!r} from router: {e!r}"
                        )
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
            try:
                writer.write(command_string)
                # Small buffer. It's possible to overload a router.
                await asyncio.sleep(0.01)
                await writer.drain()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as err:
                _LOGGER.warning(f"Writing to the router failed: {err!r}")
                self.commands_to_send.task_done()
                self._start_reconnect()
                return
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

        # connect() runs discovery; cover callers that used open() directly.
        if not self._discovery_attempted:
            await self.discover_router_ids()

        # Get Groups
        await self.get_groups()

        # Get Devices
        await self.get_devices()

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

    async def _send_command_task(self, command: Command):

        # Take an in-flight slot before sending: routers answer queries one at
        # a time, and initialising a large site fires hundreds of queries at
        # once. Without this the router falls behind until replies arrive
        # after the timeout (or not at all). The response timeout starts once
        # the command is actually sent, not while waiting for a slot.
        async with self._command_slots:
            return await self._send_command_locked(command)

    async def _send_command_locked(self, command: Command):

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
                        # Callers decide how serious an error reply is - e.g.
                        # probing all four subnets for devices is *expected* to
                        # error on subnets the router doesn't have.
                        _LOGGER.warning(
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

                remaining = (
                    start_time
                    + datetime.timedelta(0, COMMAND_RESPONSE_TIMEOUT)
                    - datetime.datetime.now()
                ).total_seconds()
                if remaining <= 0:
                    raise CommandResponseTimeout(command)

                # Bound the wait so the timeout fires even when the router
                # goes silent - a bare Condition.wait() only wakes when some
                # other message arrives, which used to stall unanswered
                # queries until the next keepalive.
                try:
                    await asyncio.wait_for(
                        self.command_received.wait(), remaining
                    )
                except asyncio.TimeoutError:
                    raise CommandResponseTimeout(command) from None

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
