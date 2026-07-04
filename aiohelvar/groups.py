from aiohelvar.lib import Subscribable, guarded
from aiohelvar.static import DEFAULT_FADE_TIME
from aiohelvar.parser.address import HelvarAddress, SceneAddress
import asyncio
from aiohelvar.parser.command_parameter import CommandParameter, CommandParameterType
from .parser.command_type import CommandType, MessageType
from .parser.command import Command

import logging

_LOGGER = logging.getLogger(__name__)


# QUERY_LAST_SCENE_IN_GROUP (C:109) replies with values >= 256 when no scene
# has been recalled in the group since power-up.
LAST_SCENE_NONE_SENTINEL = 256


def blockscene_to_block_and_scene(block_scene: int):
    """Decode a C:109 (query last scene in group) reply value.

    The router encodes the last scene as (block - 1) * 16 + scene with a
    1-based scene, so valid values run 1..128 (verified on a 910: 71 -> 5.7,
    15 -> 1.15, 16 -> 1.16, 18 -> 2.2). Values >= 256 are a sentinel meaning
    "no scene recalled since power-up".

    Returns a (block, scene) tuple, or None for the sentinel / invalid values.
    """
    if block_scene is None or block_scene < 1 or block_scene >= LAST_SCENE_NONE_SENTINEL:
        return None
    zero_based = block_scene - 1
    return zero_based // 16 + 1, zero_based % 16 + 1


class Group(Subscribable):
    def __init__(self, group_id: int, name=None):
        super(Group, self).__init__()
        self.group_id: int = group_id
        self.name = None
        self.devices = []
        self.last_scene_address = None

    def __str__(self):
        return f"Group {self.group_id}: {self.name}. Has {len(self.devices)} devices."

    def __hash__(self) -> int:
        return hash(self.group_id)

    def __eq__(self, o: object) -> bool:
        return self.group_id == o.group_id

    def get_last_scene_address(self):
        return self.last_scene_address

    def get_levels_for_scene(self, scene_address):
        pass
        # TODO
        # levels = {}

        # for device in self.devices:
        #     levels[device.address] = device.level_for_scene(scene_address)

        # return levels


class Groups:
    def __init__(self, router):
        self.router = router
        self.groups = {}

    def register_group(self, group: Group):
        self.groups[int(group.group_id)] = group

    def update_group_name(self, group_id: int, name):
        self.groups[int(group_id)].name = name

    def update_group_device_members(self, group_id: int, addresses):
        self.groups[int(group_id)].devices = addresses

    def unregister_subscription(self, group_id, func):
        group = self.groups.get(group_id)

        if group:
            group.remove_subscriber(func)
            return True
        return False

    def register_subscription(self, group_id: int, func):

        group = self.groups.get(int(group_id))

        if group:
            group.add_subscriber(func)
            return True
        return False

    def get_scenes_for_group(self, group_id, only_named=True):
        return self.router.scenes.get_scenes_for_group(group_id, only_named)

    async def force_update_groups(self):
        """Force subscription updates for all groups"""
        [await group.update_subscribers() for group in self.groups.values()]

    async def handle_scene_callback(self, scene_address: SceneAddress, fade_time):

        if scene_address.group not in self.groups.keys():
            _LOGGER.info(
                f"Scene {scene_address} not in any known group. Looking for {scene_address.group} in {self.groups.keys()}. Ignoring."
            )
            return

        group = self.groups.get(scene_address.group)
        if not group:
            _LOGGER.error(f"Group {scene_address.group} not found for scene {scene_address}")
            return
        group.last_scene_address = scene_address

        _LOGGER.info(
            f"Updating devices in group {group.name} to scene {scene_address}..."
        )
        for device_address in self.groups[scene_address.group].devices:
            device = self.router.devices.devices.get(device_address)
            if device is None:
                _LOGGER.warning(
                    f"Can't find device {device_address} registered in group {scene_address.group}."
                )
                continue
            await device.set_scene_level(scene_address)

        await group.update_subscribers()

        _LOGGER.info(f"Updated devices in scene {scene_address}.")

    async def set_scene(self, scene_address: SceneAddress, fade_time=DEFAULT_FADE_TIME):
        """
        Set the scene with the router.

        We'll get a scene change callback from the router that well use to update device state,
        so no need to call one here.

        """

        await self.router.send_command(
            Command(
                CommandType.RECALL_SCENE,
                [
                    CommandParameter(CommandParameterType.GROUP, scene_address.group),
                    CommandParameter(CommandParameterType.BLOCK, scene_address.block),
                    CommandParameter(CommandParameterType.SCENE, scene_address.scene),
                    CommandParameter(CommandParameterType.FADE_TIME, fade_time),
                ],
            )
        )

    async def set_group_level(self, group_id: int, level, fade_time=DEFAULT_FADE_TIME):
        """Set all channels in a group directly to a level (C:13).

        Sends ">V:2,C:13,G:<group>,L:<0-100>,F:<fade>#"; the router sends no
        reply. ``fade_time`` is in HelvarNet units of 1/100 s. Unlike a scene
        recall, a direct level also drives channels whose scene-table entry is
        "*" (ignore scene command), so this can force a whole group to a level
        - e.g. 0 to switch it off.
        """
        level = float(level)
        if level < 0:
            level = 0
        if level > 100:
            level = 100
        # HelvarNet levels are 0-100; send ints as ints to keep the wire clean.
        if level == int(level):
            level = int(level)

        await self.router.send_command(
            Command(
                CommandType.DIRECT_LEVEL_GROUP,
                [
                    CommandParameter(CommandParameterType.GROUP, int(group_id)),
                    CommandParameter(CommandParameterType.LEVEL, level),
                    CommandParameter(CommandParameterType.FADE_TIME, fade_time),
                ],
            )
        )


async def get_groups(router):

    response = await router._send_command_task(Command(CommandType.QUERY_GROUPS))

    # We expect a comma separated list of group ids.
    async def update_name(router, group_id):
        response = await router._send_command_task(
            Command(
                CommandType.QUERY_GROUP_DESCRIPTION,
                [CommandParameter(CommandParameterType.GROUP, group_id)],
            )
        )
        if response.command_message_type != MessageType.REPLY:
            _LOGGER.warning(
                f"QUERY_GROUP_DESCRIPTION for group {group_id} did not "
                f"return a reply: {response}"
            )
            return
        router.groups.update_group_name(group_id, response.result)

    async def update_group_devices(router, group_id):
        response = await router._send_command_task(
            Command(
                CommandType.QUERY_GROUP,
                [CommandParameter(CommandParameterType.GROUP, group_id)],
            )
        )

        if response.command_message_type != MessageType.REPLY:
            # An error reply carries the error code as its result - don't
            # parse it as a device list.
            _LOGGER.warning(
                f"QUERY_GROUP for group {group_id} did not return a reply: {response}"
            )
            return

        if response.result is not None:
            members = [member.strip("@") for member in response.result.split(",")]
            _LOGGER.debug(f"members is '{members}'")

            addresses = [HelvarAddress(*member.split(".")) for member in members]
            _LOGGER.debug(f"addresses is '{addresses}'")

            router.groups.update_group_device_members(group_id, addresses)

    async def update_group_last_scene(router, group_id):
        response = await router._send_command_task(
            Command(
                CommandType.QUERY_LAST_SCENE_IN_GROUP,
                [CommandParameter(CommandParameterType.GROUP, group_id)],
            )
        )

        if response.command_message_type != MessageType.REPLY:
            # An error reply carries the error code as its result - it must
            # never be decoded as a block/scene value.
            _LOGGER.warning(
                f"QUERY_LAST_SCENE_IN_GROUP for group {group_id} did not "
                f"return a reply: {response}"
            )
            return

        try:
            block_scene = int(response.result)
        except (ValueError, TypeError):
            _LOGGER.error(f"Invalid block_scene value: {response.result}")
            return
        block_and_scene = blockscene_to_block_and_scene(block_scene)
        if block_and_scene is None:
            _LOGGER.debug(
                f"Group {group_id} has no last scene (C:109 returned {block_scene})."
            )
            return
        scene_address = SceneAddress(group_id, *block_and_scene)
        await router.groups.handle_scene_callback(scene_address, 10)

    if not response.result:
        _LOGGER.debug(
            "Response to QUERY_GROUPS command was empty. Assuming no groups defined."
        )
        return

    # TODO: Validate input - Regex for comma separated ints would do

    try:
        group_ids = response.result.split(",")
        groups = []
        for group_id in group_ids:
            group_id = group_id.strip()
            if group_id:  # Skip empty strings
                try:
                    # Validate that group_id is numeric
                    int(group_id)
                    groups.append(Group(group_id))
                except ValueError:
                    _LOGGER.warning(f"Invalid group ID: {group_id}")
    except AttributeError:
        _LOGGER.error("Response result is not a string - cannot parse groups")
        return

    for group in groups:
        router.groups.register_group(group)
        asyncio.create_task(
            guarded(
                update_name(router, group.group_id),
                f"Querying name of group {group.group_id}",
                _LOGGER,
            )
        )
        asyncio.create_task(
            guarded(
                update_group_devices(router, group.group_id),
                f"Querying devices of group {group.group_id}",
                _LOGGER,
            )
        )
        asyncio.create_task(
            guarded(
                update_group_last_scene(router, group.group_id),
                f"Querying last scene of group {group.group_id}",
                _LOGGER,
            )
        )
