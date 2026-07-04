"""Regression tests for the failure modes observed on a real 910 install.

Observed in Home Assistant logs (integration setup against real hardware):

* ``CommandResponseTimeout: >V:2,C:164,G:1#`` as an unretrieved task
  exception - concurrent queries of the same command type could steal each
  other's replies because matching ignored the parameters, and the failing
  task was fire-and-forget.
* Setup stalling for minutes and finally being cancelled inside
  ``get_scenes`` - unanswered queries only timed out when *some* other
  message arrived, and scene-name queries had no per-query bound.
* ``!V:2,C:100,@1.1.3=23#`` logged as an ERROR - probing subnets a router
  doesn't have (S-DIM/DMX on a 910) is expected and must be quiet-ish.
"""

import asyncio

import pytest

import aiohelvar.router as router_module
from aiohelvar.exceptions import CommandResponseTimeout
from aiohelvar.groups import Group, get_groups
from aiohelvar.mock_router import MODERN, FirmwareProfile, MockRouter
from aiohelvar.parser.address import HelvarAddress
from aiohelvar.parser.command import Command
from aiohelvar.parser.command_parameter import CommandParameter, CommandParameterType
from aiohelvar.parser.command_type import CommandType
from aiohelvar.parser.parser import CommandParser
from aiohelvar.router import Router
from aiohelvar.scenes import get_scenes


def _group_query(group_id):
    return Command(
        CommandType.QUERY_GROUP,
        [CommandParameter(CommandParameterType.GROUP, group_id)],
    )


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --- reply matching includes parameters ------------------------------------


class TestReplyMatching:
    def test_matching_key_includes_parameters(self):
        request_g1 = _group_query(1)
        request_g2 = _group_query(2)
        assert (
            request_g1.type_parameters_address != request_g2.type_parameters_address
        )

        reply_g1 = CommandParser().parse_command(b"?V:2,C:164,G:1=@0.1.1.1#")
        assert reply_g1.type_parameters_address == request_g1.type_parameters_address
        assert reply_g1.type_parameters_address != request_g2.type_parameters_address

    def test_matching_key_includes_address(self):
        with_address = Command(
            CommandType.QUERY_ROUTERS, command_address=HelvarAddress(0)
        )
        bare = Command(CommandType.QUERY_ROUTERS)
        assert with_address.type_parameters_address != bare.type_parameters_address

    @pytest.mark.asyncio
    async def test_concurrent_group_queries_get_their_own_replies(self):
        async with MockRouter(MODERN, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                responses = await asyncio.gather(
                    router._send_command_task(_group_query(1)),
                    router._send_command_task(_group_query(2)),
                )
            finally:
                await router.disconnect()

        by_group = {
            r.get_param_value(CommandParameterType.GROUP): r.result for r in responses
        }
        # The mock stores distinct member lists per group.
        assert by_group == {"1": "@0.1.1.1,@0.1.1.2", "2": "@0.1.1.2"}


# --- timeouts fire even when the router goes silent -------------------------


class TestSilentRouter:
    @pytest.mark.asyncio
    async def test_command_times_out_without_any_other_traffic(self, monkeypatch):
        monkeypatch.setattr(router_module, "COMMAND_RESPONSE_TIMEOUT", 1)
        profile = FirmwareProfile(
            name="silent-group-query",
            results=dict(MODERN.results),
            silent_commands=frozenset({CommandType.QUERY_GROUP.command_id}),
        )
        async with MockRouter(profile, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                # No keepalive is running and nothing else is on the wire, so
                # the timeout must fire on its own (used to hang until the
                # next unrelated message arrived).
                with pytest.raises(CommandResponseTimeout):
                    await asyncio.wait_for(
                        router._send_command_task(_group_query(1)), timeout=5
                    )
            finally:
                await router.disconnect()

    @pytest.mark.asyncio
    async def test_get_scenes_survives_unanswered_scene_name_queries(self, monkeypatch):
        monkeypatch.setattr(router_module, "COMMAND_RESPONSE_TIMEOUT", 0.5)
        profile = FirmwareProfile(
            name="silent-scene-names",
            results=dict(MODERN.results),
            silent_commands=frozenset({CommandType.QUERY_SCENE_NAMES.command_id}),
        )
        async with MockRouter(profile, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                router.groups.register_group(Group(1))
                # Must complete despite the router never answering C:166.
                await asyncio.wait_for(get_scenes(router, router.groups), timeout=5)
            finally:
                await router.disconnect()

        # Placeholders exist, just without names.
        scenes = router.scenes.get_scenes_for_group(1, only_named=False)
        assert scenes
        assert all(scene.name is None for scene in scenes)


# --- error replies during initialisation ------------------------------------


class TestErrorReplies:
    @pytest.mark.asyncio
    async def test_group_queries_with_error_replies_do_not_crash_tasks(self):
        """Firmware rejecting C:105/C:164/C:109 must not corrupt group state."""
        profile = FirmwareProfile(
            name="grumpy",
            results=dict(MODERN.results),
            unsupported_commands=frozenset(
                {
                    CommandType.QUERY_GROUP_DESCRIPTION.command_id,
                    CommandType.QUERY_GROUP.command_id,
                    CommandType.QUERY_LAST_SCENE_IN_GROUP.command_id,
                }
            ),
        )
        async with MockRouter(profile, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                await get_groups(router)
                await _wait_for(
                    lambda: len(
                        [
                            c
                            for c in mock.received_commands
                            if c.command_type
                            in (
                                CommandType.QUERY_GROUP,
                                CommandType.QUERY_GROUP_DESCRIPTION,
                                CommandType.QUERY_LAST_SCENE_IN_GROUP,
                            )
                        ]
                    )
                    >= 6
                )
                await asyncio.sleep(0.1)
            finally:
                await router.disconnect()

        group = router.groups.groups[1]
        # The error code must not leak into the group state: no name set,
        # no devices parsed from "15", no last scene decoded from "15".
        assert group.name is None
        assert group.devices == []
        assert group.last_scene_address is None

    @pytest.mark.asyncio
    async def test_missing_subnet_probe_is_not_fatal(self):
        """Error 23 on @c.r.3/@c.r.4 (no S-DIM/DMX) must register nothing."""
        profile = FirmwareProfile(
            name="no-devices",
            results=dict(MODERN.results),
            unsupported_commands=frozenset(
                {CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES.command_id}
            ),
            error_code=23,  # undocumented code seen on a real 910
        )
        async with MockRouter(profile, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                await router.get_devices()
                await _wait_for(
                    lambda: len(
                        [
                            c
                            for c in mock.received_commands
                            if c.command_type
                            is CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES
                        ]
                    )
                    >= 4
                )
                await asyncio.sleep(0.1)
            finally:
                await router.disconnect()

        assert router.devices.devices == {}


# --- flood control ----------------------------------------------------------


class TestFloodControl:
    @pytest.mark.asyncio
    async def test_concurrent_commands_are_throttled(self, monkeypatch):
        """No more than MAX_CONCURRENT_COMMANDS queries may be in flight.

        Reproduces the mass-timeout seen on a real site with ~20 groups and
        60+ devices: hundreds of concurrent start-up queries flooded the
        router until replies arrived late or not at all.
        """
        monkeypatch.setattr(router_module, "COMMAND_RESPONSE_TIMEOUT", 1)
        profile = FirmwareProfile(
            name="silent-state",
            results=dict(MODERN.results),
            silent_commands=frozenset({CommandType.QUERY_DEVICE_STATE.command_id}),
        )
        async with MockRouter(profile, port=0) as mock:
            router = Router(mock.host, mock.port)
            await router.open()
            try:
                queries = [
                    asyncio.create_task(
                        router._send_command_task(
                            Command(
                                CommandType.QUERY_DEVICE_STATE,
                                command_address=HelvarAddress(0, 1, 1, device),
                            )
                        )
                    )
                    for device in range(1, 11)
                ]
                # Give the writer time to send everything it is allowed to.
                await asyncio.sleep(0.5)
                in_flight = [
                    c
                    for c in mock.received_commands
                    if c.command_type is CommandType.QUERY_DEVICE_STATE
                ]
                assert len(in_flight) == router_module.MAX_CONCURRENT_COMMANDS

                results = await asyncio.gather(*queries, return_exceptions=True)
                assert all(isinstance(r, CommandResponseTimeout) for r in results)
            finally:
                await router.disconnect()


# --- reader robustness ------------------------------------------------------


async def _start_raw_server(payloads):
    """A one-shot TCP server that answers the first read with ``payloads``."""

    async def handle(reader, writer):
        await reader.readuntil(b"#")
        for payload in payloads:
            writer.write(payload)
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class TestReaderRobustness:
    def test_parser_decodes_legacy_8bit_names(self):
        # "Küche" in latin-1 - not valid UTF-8. Must not raise.
        command = CommandParser().parse_command(b"?V:2,C:106,@0.1.1.1=K\xfcche#")
        assert command.result == "Küche"

    @pytest.mark.asyncio
    async def test_reader_survives_garbage_and_legacy_encoding(self):
        """Unparseable lines and non-UTF-8 bytes must not kill the reader."""
        server, port = await _start_raw_server(
            [
                b"\xff\xfe***garbage***#",  # undecodable + unparseable line
                b"?V:2,C:107=K\xfcche#",  # latin-1 reply to the actual query
            ]
        )
        try:
            router = Router("127.0.0.1", port)
            await router.open()
            try:
                response = await router.query(
                    Command(CommandType.QUERY_WORKGROUP_NAME), timeout=5
                )
                assert response.result == "Küche"
            finally:
                await router.disconnect()
        finally:
            server.close()
            await server.wait_closed()


# --- automatic reconnect ----------------------------------------------------


class TestAutoReconnect:
    @pytest.mark.asyncio
    async def test_connection_loss_triggers_reconnect(self, monkeypatch):
        monkeypatch.setattr(router_module, "RECONNECT_RETRY_DELAY", 0.2)

        mock = await MockRouter(MODERN, port=0).start()
        port = mock.port
        router = Router(mock.host, port)
        await router.connect()
        assert router.connected is True

        # Simulate a router reboot: drop the connection, come back later.
        await mock.stop()
        assert await _wait_for(lambda: router.connected is False)

        mock2 = await MockRouter(MODERN, host=mock.host, port=port).start()
        try:
            assert await _wait_for(lambda: router.connected is True)
            # The reconnected session works: queries get answered again.
            response = await router.query(
                Command(CommandType.QUERY_WORKGROUP_NAME), timeout=5
            )
            assert response.result == "MockWorkgroup"
        finally:
            await router.disconnect()
            await mock2.stop()

    @pytest.mark.asyncio
    async def test_deliberate_disconnect_stops_reconnect_attempts(self, monkeypatch):
        monkeypatch.setattr(router_module, "RECONNECT_RETRY_DELAY", 0.2)

        mock = await MockRouter(MODERN, port=0).start()
        router = Router(mock.host, mock.port)
        await router.connect()

        # Drop the connection and let the retry loop start (the mock stays
        # down, so it keeps retrying).
        await mock.stop()
        assert await _wait_for(
            lambda: router._reconnect_task is not None
            and not router._reconnect_task.done()
        )

        await router.disconnect()
        assert await _wait_for(
            lambda: router._reconnect_task.done() or router._reconnect_task.cancelled()
        )
        assert router.connected is False
