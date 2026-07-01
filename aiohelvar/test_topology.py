"""Tests for cluster/router topology discovery and multi-router enumeration."""

import asyncio

import pytest

from aiohelvar.devices import get_devices
from aiohelvar.mock_router import MODERN, FirmwareProfile, MockRouter
from aiohelvar.parser.command_type import CommandType
from aiohelvar.router import Router


@pytest.mark.asyncio
async def test_discover_topology_lists_routers():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)  # 127.0.0.1 -> cluster 0, router 1
        await router.open()
        try:
            topology = await router.discover_topology(timeout=2.0)
        finally:
            await router.disconnect()

    # Mock reports routers "1,2" in the connected cluster (0).
    assert topology == {0: [1, 2]}


@pytest.mark.asyncio
async def test_discover_topology_falls_back_when_unsupported():
    # Firmware that rejects QUERY_ROUTERS must not lose the connected router.
    profile = FirmwareProfile(
        name="no-routers-query",
        unsupported_commands=frozenset({CommandType.QUERY_ROUTERS.command_id}),
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            topology = await router.discover_topology(timeout=2.0)
        finally:
            await router.disconnect()

    assert topology == {0: [1]}  # just the connected router


@pytest.mark.asyncio
async def test_get_devices_enumerates_all_routers():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            await get_devices(router)
            # get_devices fire-and-forgets the per-router probes; let them run.
            await asyncio.sleep(0.3)
        finally:
            await router.disconnect()

    discovery = [
        c
        for c in mock.received_commands
        if c.command_type == CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES
    ]
    probed_routers = {c.command_address.router for c in discovery if c.command_address}
    # Both routers in the cluster were probed, not just the connected one.
    assert probed_routers == {1, 2}


@pytest.mark.asyncio
async def test_diagnostics_reports_routers():
    from aiohelvar.diagnostics import run_diagnostics

    async with MockRouter(MODERN, port=0) as mock:
        report = await run_diagnostics(mock.host, mock.port, timeout=2.0)

    assert report.routers == "1,2"
    assert report.to_dict()["routers"] == "1,2"
