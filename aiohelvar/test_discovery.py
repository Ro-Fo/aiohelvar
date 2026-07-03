"""Tests for runtime cluster/router discovery, scene-name parsing, the C:109
scene decoding, and group-level direct control.

All behaviours mirrored by the MockRouter here were verified against a real
Helvar 910 router (firmware 4.3.1.0, HelvarNet v3):

* a bare C:102 is answered with error 17 ("Missing ASCII parameter"); the
  cluster must be passed as the address parameter (">V:2,C:102,@<c>#"),
* C:166 scene-name entries are separated by ",@" and names may contain ":",
* C:109 encodes the last scene as (block-1)*16 + scene (1-based), with
  values >= 256 meaning "no scene since power-up",
* C:190 replies with a packed 32-bit version int (67305728 == 4.3.1.0).
"""

import asyncio

import pytest

from aiohelvar.diagnostics import decode_packed_version
from aiohelvar.groups import Group
from aiohelvar.lib import parse_id_list
from aiohelvar.mock_router import (
    LAST_SCENE_NONE,
    MODERN,
    FirmwareProfile,
    MockRouter,
    pack_version,
)
from aiohelvar.parser.address import HelvarAddress, SceneAddress
from aiohelvar.parser.command import Command
from aiohelvar.parser.command_parameter import CommandParameter, CommandParameterType
from aiohelvar.parser.command_type import CommandType
from aiohelvar.parser.parser import CommandParser
from aiohelvar.router import Router
from aiohelvar.scenes import Scene, get_scenes, parse_scene_names


# --- helpers --------------------------------------------------------------


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --- cluster-only addresses (@c) ------------------------------------------


def test_cluster_only_address_serialises_without_router():
    assert str(HelvarAddress(0)) == "@0"
    assert str(HelvarAddress(253)) == "@253"


def test_cluster_only_address_parses_from_wire():
    command = CommandParser().parse_command(b"?V:2,C:102,@0=1,2#")
    assert command.command_address == HelvarAddress(0)
    assert command.result == "1,2"


def test_parse_id_list():
    assert parse_id_list("1,2,3") == [1, 2, 3]
    assert parse_id_list("@1,@3") == [1, 3]
    assert parse_id_list("0") == [0]
    assert parse_id_list("") == []
    assert parse_id_list(None) == []
    assert parse_id_list("1, x, 2") == [1, 2]


# --- mock realism: C:102 --------------------------------------------------


class TestMockRouters:
    def test_bare_c102_returns_error_17(self):
        # Real firmware: "Missing ASCII parameter".
        response = MockRouter(MODERN).build_response(Command(CommandType.QUERY_ROUTERS))
        assert response == "!V:2,C:102=17#"

    def test_c102_with_cluster_address_returns_router_list(self):
        response = MockRouter(MODERN).build_response(
            Command(CommandType.QUERY_ROUTERS, command_address=HelvarAddress(0))
        )
        assert response == "?V:2,C:102,@0=1,2#"

    def test_c102_with_unknown_cluster_returns_error_9(self):
        # Real firmware: "Cluster does not exist" - exactly what happens when
        # the IP-octet heuristic probes a 192.168.x.y address.
        response = MockRouter(MODERN).build_response(
            Command(CommandType.QUERY_ROUTERS, command_address=HelvarAddress(168))
        )
        assert response == "!V:2,C:102,@168=9#"


# --- mock realism: C:109 and C:190 ----------------------------------------


class TestMockLastSceneAndVersion:
    def test_last_scene_uses_verified_encoding(self):
        response = MockRouter(MODERN).build_response(
            Command(
                CommandType.QUERY_LAST_SCENE_IN_GROUP,
                [CommandParameter(CommandParameterType.GROUP, 1)],
            )
        )
        assert response == "?V:2,C:109,G:1=71#"  # block 5, scene 7

    def test_last_scene_sentinel_for_untouched_group(self):
        response = MockRouter(MODERN).build_response(
            Command(
                CommandType.QUERY_LAST_SCENE_IN_GROUP,
                [CommandParameter(CommandParameterType.GROUP, 2)],
            )
        )
        assert response == f"?V:2,C:109,G:2={LAST_SCENE_NONE}#"

    def test_router_version_is_packed_int(self):
        response = MockRouter(MODERN).build_response(
            Command(CommandType.QUERY_ROUTER_VERSION)
        )
        assert response == f"?V:2,C:190={pack_version(5, 4, 2, 0)}#"


def test_decode_packed_version():
    assert decode_packed_version(67305728) == "4.3.1.0"  # verified on a 910
    assert decode_packed_version("67305728") == "4.3.1.0"
    assert decode_packed_version(pack_version(5, 4, 2, 0)) == "5.4.2.0"
    assert decode_packed_version("2.3.1") is None  # legacy dotted string
    assert decode_packed_version(None) is None
    assert decode_packed_version(-1) is None
    assert decode_packed_version(2**32 + 1) is None


# --- runtime discovery ----------------------------------------------------


@pytest.mark.asyncio
async def test_router_discovers_cluster_and_router_ids_at_runtime():
    async with MockRouter(MODERN, port=0) as mock:
        # 127.0.0.1 would make the IP heuristic guess cluster 0 / router 1,
        # but the point is that the ids come from C:101/C:102 now.
        router = Router(mock.host, mock.port)
        await router.connect()
        try:
            assert router.ids_discovered is True
            assert router.cluster_id == 0
            assert router.router_id == 1
            assert router.clusters == [0]
            assert router.cluster_routers == {0: [1, 2]}
        finally:
            await router.disconnect()

        # The C:102 queries carried the cluster as their address parameter.
        c102 = [
            c
            for c in mock.received_commands
            if c.command_type is CommandType.QUERY_ROUTERS
        ]
        assert c102, "expected at least one QUERY_ROUTERS command"
        assert all(c.command_address == HelvarAddress(0) for c in c102)


@pytest.mark.asyncio
async def test_use_specified_ids_skips_discovery():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(
            mock.host, mock.port, cluster_id=7, router_id=3, use_specified_ids=True
        )
        await router.connect()
        try:
            assert router.cluster_id == 7
            assert router.router_id == 3
            assert router.ids_discovered is False
        finally:
            await router.disconnect()

        assert not any(
            c.command_type in (CommandType.QUERY_CLUSTERS, CommandType.QUERY_ROUTERS)
            for c in mock.received_commands
        )


@pytest.mark.asyncio
async def test_discovery_falls_back_to_ip_heuristic_when_queries_fail():
    profile = FirmwareProfile(
        name="no-discovery",
        unsupported_commands=frozenset(
            {CommandType.QUERY_CLUSTERS.command_id, CommandType.QUERY_ROUTERS.command_id}
        ),
        results=dict(MODERN.results),
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        heuristic = (router.cluster_id, router.router_id)
        await router.connect()
        try:
            assert router.ids_discovered is False
            assert (router.cluster_id, router.router_id) == heuristic
        finally:
            await router.disconnect()


# --- scene-name parsing ----------------------------------------------------


class TestParseSceneNames:
    def test_entries_are_separated_by_comma_at(self):
        # A naive split("@") leaves a trailing "," on every name but the last.
        names = parse_scene_names("@1.1.1:Day,@1.1.2:Night,@1.1.3:Evening")
        assert names == {
            SceneAddress(1, 1, 1): "Day",
            SceneAddress(1, 1, 2): "Night",
            SceneAddress(1, 1, 3): "Evening",
        }
        assert not any(name.endswith(",") for name in names.values())

    def test_names_may_contain_colons(self):
        names = parse_scene_names("@1.1.1:Dinner: late,@1.1.2:A:B:C")
        assert names[SceneAddress(1, 1, 1)] == "Dinner: late"
        assert names[SceneAddress(1, 1, 2)] == "A:B:C"

    def test_names_may_contain_commas(self):
        names = parse_scene_names("@1.1.1:Eat, drink,@1.1.2:Sleep")
        assert names[SceneAddress(1, 1, 1)] == "Eat, drink"
        assert names[SceneAddress(1, 1, 2)] == "Sleep"

    def test_invalid_entries_are_skipped(self):
        # "@junk" has no name separator; block 999 is out of range (1-253).
        names = parse_scene_names("@1.1.1:Day,@junk,@1.999.1:Bad block,@1.1.2:Night")
        assert names == {
            SceneAddress(1, 1, 1): "Day",
            SceneAddress(1, 1, 2): "Night",
        }

    def test_empty_and_none_payloads(self):
        assert parse_scene_names(None) == {}
        assert parse_scene_names("") == {}
        assert parse_scene_names(12) == {}


@pytest.mark.asyncio
async def test_get_scenes_merges_bare_and_per_group_queries():
    """A bare C:166 returns only a subset on real firmware; per-group queries
    with a G: parameter fill in the rest."""
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.connect()
        try:
            router.groups.register_group(Group(1))
            router.groups.register_group(Group(2))
            await get_scenes(router, router.groups)
        finally:
            await router.disconnect()

    assert router.scenes.scenes[SceneAddress(1, 1, 1)].name == "Mock day"
    # Only present in the per-group reply, and contains a ":".
    assert router.scenes.scenes[SceneAddress(1, 1, 2)].name == "Mock night: late"
    assert router.scenes.scenes[SceneAddress(2, 1, 1)].name == "Mock other"

    # The router was queried bare AND once per group.
    c166 = [
        c
        for c in mock.received_commands
        if c.command_type is CommandType.QUERY_SCENE_NAMES
    ]
    groups_queried = {
        c.get_param_value(CommandParameterType.GROUP) for c in c166
    }
    # Parameters arrive as strings off the wire; None is the bare query.
    assert groups_queried == {None, "1", "2"}


# --- unnamed scenes / display names ----------------------------------------


def test_scene_display_name_falls_back_for_unnamed_scenes():
    named = Scene(SceneAddress(1, 1, 1), name="Day")
    unnamed = Scene(SceneAddress(1, 2, 7))
    assert named.display_name == "Day"
    assert unnamed.display_name == "Scene 2.7"


@pytest.mark.asyncio
async def test_selectable_scenes_include_unnamed_scenes_in_use():
    """Unnamed scenes count as selectable when a device in the group stores a
    concrete level for them (not "*" / unknown)."""
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.connect()
        try:
            await router.initialize()
            # Device names/levels are fetched by background tasks.
            assert await _wait_for(
                lambda: all(
                    d.levels for d in router.devices.devices.values() if d.is_load
                )
            )

            selectable = router.scenes.get_selectable_scenes_for_group(1)
            addresses = {str(s.address) for s in selectable}

            # Named scenes are always present...
            assert "@1.1.1" in addresses
            assert "@1.1.2" in addresses
            # ...unnamed scenes with real levels in the mock scene table too
            # (block 1 scene 15/16 and block 2 scene 2)...
            assert "@1.1.15" in addresses
            assert "@1.1.16" in addresses
            assert "@1.2.2" in addresses
            # ...but not the thousands of untouched block/scene combinations.
            assert "@1.3.1" not in addresses
            assert len(selectable) < 20

            named_only = router.scenes.get_selectable_scenes_for_group(
                1, include_unnamed=False
            )
            assert {str(s.address) for s in named_only} == {"@1.1.1", "@1.1.2"}
        finally:
            await router.disconnect()


# --- group-level direct control (C:13) -------------------------------------


def test_direct_level_group_wire_format():
    command = Command(
        CommandType.DIRECT_LEVEL_GROUP,
        [
            CommandParameter(CommandParameterType.GROUP, 1),
            CommandParameter(CommandParameterType.LEVEL, 50),
            CommandParameter(CommandParameterType.FADE_TIME, 50),
        ],
    )
    assert str(command) == ">V:2,C:13,G:1,L:50,F:50#"


@pytest.mark.asyncio
async def test_set_group_level_sends_c13_and_expects_no_reply():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.connect()
        try:
            await router.groups.set_group_level(1, 42, fade_time=100)
            assert await _wait_for(
                lambda: any(
                    c.command_type is CommandType.DIRECT_LEVEL_GROUP
                    for c in mock.received_commands
                )
            )
        finally:
            await router.disconnect()

    c13 = [
        c
        for c in mock.received_commands
        if c.command_type is CommandType.DIRECT_LEVEL_GROUP
    ][0]
    assert c13.get_param_value(CommandParameterType.GROUP) == "1"
    assert c13.get_param_value(CommandParameterType.LEVEL) == "42"
    assert c13.get_param_value(CommandParameterType.FADE_TIME) == "100"


@pytest.mark.asyncio
async def test_set_group_level_clamps_out_of_range_levels():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.connect()
        try:
            await router.groups.set_group_level(1, 250)
            await router.groups.set_group_level(1, -5)
            assert await _wait_for(
                lambda: len(
                    [
                        c
                        for c in mock.received_commands
                        if c.command_type is CommandType.DIRECT_LEVEL_GROUP
                    ]
                )
                >= 2
            )
        finally:
            await router.disconnect()

    levels = [
        c.get_param_value(CommandParameterType.LEVEL)
        for c in mock.received_commands
        if c.command_type is CommandType.DIRECT_LEVEL_GROUP
    ]
    assert levels == ["100", "0"]
