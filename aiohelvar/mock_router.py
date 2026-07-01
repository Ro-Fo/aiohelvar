"""A configurable mock HelvarNet router for testing without real hardware.

The mock is an asyncio TCP server that speaks just enough of the HelvarNet
ASCII protocol to answer the read-only queries used by the diagnostics and by
the integration's start-up. Its behaviour is driven by a :class:`FirmwareProfile`
so you can reproduce, on demand, the two situations that matter:

* ``MODERN``  - a healthy router that answers every query.
* ``LEGACY``  - an old firmware that answers version/cluster queries but rejects
  device discovery, workgroup name and group enumeration with error code 15
  ("Invalid message command"). This is the "it just hangs / finds nothing"
  scenario reported against older routers.

You can flip between profiles while the server is running (``set_profile`` in
code, or by sending SIGHUP to the process from the console), which makes it easy
to test how a client behaves when a router's capabilities change.

Run it from the console::

    python -m aiohelvar mock --profile legacy --port 50000
    # ... then in another shell:
    python -m aiohelvar diagnose 127.0.0.1
    # ... flip the running mock to the other profile:
    kill -HUP <printed-pid>

No real device data is used anywhere in this module; every value is synthetic.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser.command import Command
from .parser.command_type import (
    COMMAND_TYPES_DONT_LISTEN_FOR_RESPONSE,
    CommandType,
    MessageType,
)
from .parser.parser import CommandParser
from .exceptions import ParserError

_LOGGER = logging.getLogger(__name__)

COMMAND_TERMINATOR = b"#"

# Error code returned for unsupported commands (see error_codes.py).
UNSUPPORTED_COMMAND_ERROR = 15


@dataclass
class FirmwareProfile:
    """Describes how the mock router answers queries.

    * ``unsupported_commands`` - command ids answered with an error instead of a
      reply (simulating firmware that does not implement them).
    * ``results`` - canned reply payloads keyed by command id. Any supported
      command without an explicit entry is answered with an empty result, so the
      mock never leaves a supported command unanswered.
    * ``echo_address_on_error`` - whether error replies echo the request address.
      Real routers normally do; set False to simulate the pathological case where
      the address is dropped and a naive client can't match the reply.
    """

    name: str
    unsupported_commands: frozenset = frozenset()
    results: Dict[int, str] = field(default_factory=dict)
    error_code: int = UNSUPPORTED_COMMAND_ERROR
    echo_address_on_error: bool = True


# Command ids referenced below (kept inline for readability).
_C_CLUSTERS = CommandType.QUERY_CLUSTERS.command_id  # 101
_C_ROUTERS = CommandType.QUERY_ROUTERS.command_id  # 102
_C_GROUP_DESC = CommandType.QUERY_GROUP_DESCRIPTION.command_id  # 105
_C_DEVICE_DESC = CommandType.QUERY_DEVICE_DESCRIPTION.command_id  # 106
_C_DEVICE_DISCOVERY = CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES.command_id  # 100
_C_DEVICE_STATE = CommandType.QUERY_DEVICE_STATE.command_id  # 110
_C_WORKGROUP = CommandType.QUERY_WORKGROUP_NAME.command_id  # 107
_C_LOAD_LEVEL = CommandType.QUERY_DEVICE_LOAD_LEVEL.command_id  # 152
_C_ROUTER_TIME = CommandType.QUERY_ROUTER_TIME.command_id  # 185
_C_GROUP = CommandType.QUERY_GROUP.command_id  # 164
_C_GROUPS = CommandType.QUERY_GROUPS.command_id  # 165
_C_ROUTER_VERSION = CommandType.QUERY_ROUTER_VERSION.command_id  # 190
_C_HELVARNET_VERSION = CommandType.QUERY_HELVARNET_VERSION.command_id  # 191
_C_DALI2_ENERGY = CommandType.QUERY_DALI2_ENERGY.command_id  # 252
_C_DALI2_DIAGNOSTICS = CommandType.QUERY_DALI2_DIAGNOSTICS.command_id  # 253


# Synthetic, generic reply payloads shared by the built-in profiles.
_MODERN_RESULTS = {
    _C_WORKGROUP: "MockWorkgroup",
    _C_ROUTER_VERSION: "5.4.2",
    _C_HELVARNET_VERSION: "2",
    _C_CLUSTERS: "1",
    _C_ROUTERS: "1,2",  # two routers in the cluster
    _C_GROUPS: "1,2",
    # type@device pairs; values are synthetic (a DALI load and a DALI switch).
    _C_DEVICE_DISCOVERY: "1@1,1@2",
    _C_GROUP: "@1.1.1.1,@1.1.1.2",
    _C_GROUP_DESC: "Mock Group",
    _C_DEVICE_DESC: "Mock Device",
    _C_DEVICE_STATE: "0",
    _C_LOAD_LEVEL: "0.0",
    _C_ROUTER_TIME: "0",
    # Synthetic DALI-2 payloads; APPP is -1 (unsupported bank) to exercise sentinels.
    _C_DALI2_ENERGY: "ACTE:1.234,ACTP:5.678,APPE:4.321,APPP:-1,ACTEL:9.001,ACTPL:1.009",
    _C_DALI2_DIAGNOSTICS: "LSF:0,LSTL:-2,CGTL:42.0",
}

MODERN = FirmwareProfile(name="modern", unsupported_commands=frozenset(), results=_MODERN_RESULTS)

# Old firmware: answers version and cluster queries, but rejects the newer
# device-discovery / workgroup / group-enumeration queries with error 15.
LEGACY = FirmwareProfile(
    name="legacy",
    unsupported_commands=frozenset({_C_DEVICE_DISCOVERY, _C_WORKGROUP, _C_GROUPS}),
    results={
        _C_ROUTER_VERSION: "2.3.1",
        _C_HELVARNET_VERSION: "1",
        _C_CLUSTERS: "1",
    },
)

BUILTIN_PROFILES = {p.name: p for p in (MODERN, LEGACY)}


class MockRouter:
    """A minimal, configurable HelvarNet router server for tests and manual use."""

    def __init__(
        self,
        profile: FirmwareProfile = MODERN,
        host: str = "127.0.0.1",
        port: int = 50000,
    ):
        self.profile = profile
        self.host = host
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None
        # Every command received, for assertions in tests (e.g. read-only checks).
        self.received_commands: List[Command] = []

    def set_profile(self, profile: FirmwareProfile) -> None:
        _LOGGER.info("Mock router switching profile: %s -> %s", self.profile.name, profile.name)
        self.profile = profile

    async def start(self) -> "MockRouter":
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        # Resolve the real port when started on port 0 (ephemeral).
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.info("Mock router listening on %s:%s (profile: %s)", self.host, self.port, self.profile.name)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.info("Mock router stopped.")

    async def __aenter__(self) -> "MockRouter":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def build_response(self, command: Command) -> Optional[str]:
        """Return the wire string the mock would reply with, or None for no reply.

        State-changing commands (recall scene, direct level) get no reply, exactly
        like a real router. Unsupported commands get an error; everything else gets
        a (possibly empty) canned reply.
        """
        if command.command_type in COMMAND_TYPES_DONT_LISTEN_FOR_RESPONSE:
            return None

        command_id = command.command_type.command_id

        if command_id in self.profile.unsupported_commands:
            address = command.command_address if self.profile.echo_address_on_error else None
            response = Command(
                command.command_type,
                command_parameters=command.command_parameters,
                command_message_type=MessageType.ERROR,
                command_address=address,
                command_result=str(self.profile.error_code),
            )
            return str(response)

        result = self.profile.results.get(command_id, "")
        response = Command(
            command.command_type,
            command_parameters=command.command_parameters,
            command_message_type=MessageType.REPLY,
            command_address=command.command_address,
            command_result=result,
        )
        return str(response)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Mock router: client connected from %s", peer)
        parser = CommandParser()
        try:
            while True:
                try:
                    line = await reader.readuntil(COMMAND_TERMINATOR)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                if not line:
                    break

                _LOGGER.debug("Mock router RX: %s", line)
                # Routers may batch commands separated by '$'.
                for part in line.split(b"$"):
                    if not part:
                        continue
                    try:
                        command = parser.parse_command(part)
                    except ParserError as err:
                        _LOGGER.warning("Mock router: could not parse %r (%s)", part, err)
                        continue

                    self.received_commands.append(command)
                    response = self.build_response(command)
                    if response is not None:
                        _LOGGER.debug("Mock router TX: %s", response)
                        writer.write(response.encode("utf-8"))
                        await writer.drain()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        finally:
            _LOGGER.debug("Mock router: client %s disconnected", peer)
            writer.close()

    async def serve_forever(self, profile_cycle: Optional[List[FirmwareProfile]] = None) -> None:
        """Serve until cancelled, cycling profiles on SIGHUP (console convenience)."""
        import os
        import signal

        await self.start()
        cycle = profile_cycle or [MODERN, LEGACY]

        def _cycle_profile():
            names = [p.name for p in cycle]
            try:
                idx = names.index(self.profile.name)
            except ValueError:
                idx = -1
            self.set_profile(cycle[(idx + 1) % len(cycle)])
            print(f"[mock] profile is now: {self.profile.name}", flush=True)

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGHUP, _cycle_profile)
            print(
                f"[mock] send 'kill -HUP {os.getpid()}' to toggle profile "
                f"({', '.join(p.name for p in cycle)})",
                flush=True,
            )
        except (NotImplementedError, AttributeError):  # pragma: no cover - non-POSIX
            _LOGGER.info("SIGHUP profile toggling not available on this platform.")

        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            pass
        finally:
            await self.stop()
