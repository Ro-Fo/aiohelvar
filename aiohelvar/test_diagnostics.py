"""Tests for the read-only diagnostics, error codes and the mock router.

These tests spin up the in-process MockRouter (on an ephemeral port, no real
hardware or network) and assert that the diagnostics correctly detect a healthy
"modern" router versus a "legacy" firmware that rejects device discovery with
error 15 - and that they never hang, even when a router drops the address from
its error reply.
"""

import asyncio

import pytest

from aiohelvar.diagnostics import (
    CMD_DEVICE_DISCOVERY,
    ProbeStatus,
    run_diagnostics,
)
from aiohelvar.error_codes import (
    HelvarErrorCode,
    coerce_error_code,
    describe,
    is_unsupported_command,
)
from aiohelvar.mock_router import LEGACY, MODERN, FirmwareProfile, MockRouter
from aiohelvar.parser.address import HelvarAddress
from aiohelvar.parser.command import Command
from aiohelvar.parser.command_type import CommandType
from aiohelvar.router import Router


# --- error_codes ---------------------------------------------------------


class TestErrorCodes:
    def test_describe_known_code(self):
        assert describe(15) == "Invalid message command"
        assert describe(9) == "Cluster does not exist"
        assert describe(0) == "Success"

    def test_describe_accepts_string(self):
        # Codes arrive as strings off the wire.
        assert describe("15") == "Invalid message command"

    def test_describe_unknown_code(self):
        assert "Unknown error code" in describe(999)
        assert "Unknown error code" in describe(None)
        assert "Unknown error code" in describe("not-a-number")

    def test_is_unsupported_command(self):
        assert is_unsupported_command(15) is True
        assert is_unsupported_command("15") is True
        assert is_unsupported_command(1) is False
        assert is_unsupported_command(None) is False

    def test_coerce_error_code(self):
        assert coerce_error_code("15") == 15
        assert coerce_error_code(15) == 15
        assert coerce_error_code(None) is None
        assert coerce_error_code("x") is None

    def test_enum_values(self):
        assert HelvarErrorCode.INVALID_MESSAGE_COMMAND == 15


class TestCommandTypes:
    def test_dali2_commands_are_recognised(self):
        # DALI-2 energy/diagnostics commands from the Helvar docs are known,
        # so replies referencing them aren't rejected as unknown commands.
        assert CommandType.get_by_command_id(252) is CommandType.QUERY_DALI2_ENERGY
        assert CommandType.get_by_command_id(253) is CommandType.QUERY_DALI2_DIAGNOSTICS


# --- mock router (pure, no network) --------------------------------------


class TestMockResponses:
    def test_modern_replies_to_workgroup(self):
        response = MockRouter(MODERN).build_response(
            Command(CommandType.QUERY_WORKGROUP_NAME)
        )
        assert response == "?V:2,C:107=MockWorkgroup#"

    def test_legacy_rejects_workgroup_with_error_15(self):
        response = MockRouter(LEGACY).build_response(
            Command(CommandType.QUERY_WORKGROUP_NAME)
        )
        assert response == "!V:2,C:107=15#"

    def test_state_changing_command_gets_no_reply(self):
        # Read-only server: recall scene must not be answered (matches real routers).
        assert MockRouter(MODERN).build_response(Command(CommandType.RECALL_SCENE)) is None

    def test_error_reply_can_drop_address(self):
        profile = FirmwareProfile(
            name="broken",
            unsupported_commands=frozenset({CMD_DEVICE_DISCOVERY}),
            echo_address_on_error=False,
        )
        command = Command(
            CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES,
            command_address=HelvarAddress(0, 1, 1),
        )
        response = MockRouter(profile).build_response(command)
        assert response == "!V:2,C:100=15#"  # no @-address echoed


# --- diagnostics against the live mock -----------------------------------


@pytest.mark.asyncio
async def test_diagnostics_modern_router():
    async with MockRouter(MODERN, port=0) as mock:
        report = await run_diagnostics(mock.host, mock.port, timeout=2.0)

    assert report.reachable is True
    assert report.workgroup_name == "MockWorkgroup"
    assert report.router_version == "5.4.2"
    assert report.helvarnet_version == "2"
    assert report.supports_device_discovery is True
    level, _ = report.verdict()
    assert level == "ok"


@pytest.mark.asyncio
async def test_diagnostics_legacy_router_flags_unsupported_discovery():
    async with MockRouter(LEGACY, port=0) as mock:
        report = await run_diagnostics(mock.host, mock.port, timeout=2.0)

    assert report.reachable is True
    # Version queries still work on legacy firmware...
    assert report.router_version == "2.3.1"
    # ...but device discovery is rejected with error 15.
    assert report.supports_device_discovery is False
    probe = report.get(CMD_DEVICE_DISCOVERY)
    assert probe.status == ProbeStatus.ERROR
    assert probe.error_code == 15
    assert "Invalid message command" in probe.detail
    level, message = report.verdict()
    assert level == "warning"
    assert "device discovery" in message


@pytest.mark.asyncio
async def test_diagnostics_can_toggle_profile_at_runtime():
    """The automated version of flipping the mock back and forth."""
    async with MockRouter(MODERN, port=0) as mock:
        report = await run_diagnostics(mock.host, mock.port, timeout=2.0)
        assert report.verdict()[0] == "ok"

        mock.set_profile(LEGACY)
        report = await run_diagnostics(mock.host, mock.port, timeout=2.0)
        assert report.verdict()[0] == "warning"
        assert report.supports_device_discovery is False


@pytest.mark.asyncio
async def test_diagnostics_unreachable_host():
    # Start then stop a mock to obtain a definitely-closed port.
    mock = await MockRouter(MODERN, port=0).start()
    host, port = mock.host, mock.port
    await mock.stop()

    report = await run_diagnostics(host, port, timeout=1.0, connect_timeout=1.0)
    assert report.reachable is False
    assert report.connect_error is not None
    assert report.verdict()[0] == "error"


@pytest.mark.asyncio
async def test_diagnostics_never_hangs_on_dropped_address():
    """A router that drops the address from its error reply must not hang us."""
    profile = FirmwareProfile(
        name="broken",
        unsupported_commands=frozenset({CMD_DEVICE_DISCOVERY}),
        results=dict(MODERN.results),
        echo_address_on_error=False,
    )
    async with MockRouter(profile, port=0) as mock:
        report = await asyncio.wait_for(
            run_diagnostics(mock.host, mock.port, timeout=0.5), timeout=10.0
        )

    probe = report.get(CMD_DEVICE_DISCOVERY)
    assert probe.status == ProbeStatus.TIMEOUT
    assert report.supports_device_discovery is None
    assert report.verdict()[0] == "warning"


@pytest.mark.asyncio
async def test_diagnostics_is_read_only():
    """Diagnostics must never send a state-changing command."""
    async with MockRouter(MODERN, port=0) as mock:
        await run_diagnostics(mock.host, mock.port, timeout=2.0)
        received_types = {c.command_type for c in mock.received_commands}

    assert received_types  # sanity: we did send something
    assert CommandType.RECALL_SCENE not in received_types
    assert CommandType.DIRECT_LEVEL_DEVICE not in received_types
    # Everything we sent must be a QUERY_* command.
    assert all(t.name.startswith("QUERY_") for t in received_types)


# --- additive Router helpers ---------------------------------------------


@pytest.mark.asyncio
async def test_router_query_with_timeout_returns_reply():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            response = await router.query(
                Command(CommandType.QUERY_WORKGROUP_NAME), timeout=2.0
            )
            assert response.result == "MockWorkgroup"
        finally:
            await router.disconnect()


@pytest.mark.asyncio
async def test_router_query_times_out_instead_of_hanging():
    profile = FirmwareProfile(
        name="broken",
        unsupported_commands=frozenset({CMD_DEVICE_DISCOVERY}),
        echo_address_on_error=False,
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await router.query(
                    Command(
                        CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES,
                        command_address=HelvarAddress(0, 1, 1),
                    ),
                    timeout=0.5,
                )
        finally:
            await router.disconnect()
