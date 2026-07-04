from .exceptions import CommandResponseTimeout
from .parser.address import SceneAddress
from .parser.command import Command, CommandType
from .parser.command_parameter import CommandParameter, CommandParameterType
from .parser.command_type import MessageType
import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# Scene names are queried once per group during initialisation. Bound each
# query so a router that leaves one unanswered can't stall start-up for the
# full (much larger) command timeout per group.
SCENE_NAME_QUERY_TIMEOUT = 15


class Scene:
    def __init__(self, scene_address: SceneAddress, levels=None, name=None):
        self.name = name
        self.levels = levels
        self.address = scene_address

    @property
    def display_name(self) -> str:
        """The router-stored name, or a generated fallback for unnamed scenes.

        Routers frequently store no names at all; the fallback
        "Scene <block>.<scene>" lets downstream consumers (e.g. Home
        Assistant) list every scene regardless.
        """
        if self.name:
            return self.name
        return f"Scene {self.address.block}.{self.address.scene}"

    def __eq__(self, o: object) -> bool:
        return self.address == o.address

    def __hash__(self) -> int:
        return hash(self.address)

    def __str__(self) -> str:
        return f"{self.address}: {self.name}"


class Scenes:
    def __init__(self, router):
        self.router = router
        self.scenes = {}

    def register_scene(self, scene_address, scene):
        self.scenes[scene_address] = scene

    def update_scene_name(self, scene_address, name):
        try:
            self.scenes[scene_address].name = name
        except KeyError:
            _LOGGER.error(
                f"Cannot update scene name: Scene not found {scene_address} "
                f"(group={scene_address.group}, block={scene_address.block}, scene={scene_address.scene}). "
                f"Available scenes: {list(self.scenes.keys())}"
            )

    def get_scene(self, scene_address):
        try:
            return self.scenes[scene_address]
        except KeyError:
            
            _LOGGER.error(
                f"Scene not found: {scene_address} (group={scene_address.group}, "
                f"block={scene_address.block}, scene={scene_address.scene}). "
                f"Available scenes: {[(str(addr), hash(addr)) for addr in self.scenes.keys()]}"
            )
            return None

    def has_scene(self, scene_address):
        """Check if a scene exists"""
        return scene_address in self.scenes

    def get_scene_safe(self, scene_address, default=None):
        """Get scene with default fallback"""
        try:
            return self.scenes[scene_address]
        except KeyError:
            return default

    def get_scenes_for_group(self, group_id: int, only_named=True):

        _LOGGER.info(
            f"There are {len(self.scenes.values())} registered scenes. We are looking for scenes with group {group_id}."
        )

        named_scenes = [
            scene
            for scene in self.scenes.values()
            if scene.address.group == int(group_id) and scene.name is not None
        ]
        named_scenes.sort(key=lambda x: x.name, reverse=False)

        if only_named:
            return named_scenes

        unnamed_scenes = [
            scene
            for scene in self.scenes.values()
            if scene.address.group == int(group_id) and scene.name is None
        ]
        unnamed_scenes.sort(key=lambda x: str(x.address), reverse=False)

        return named_scenes + unnamed_scenes

    def get_selectable_scenes_for_group(self, group_id: int, include_unnamed=True):
        """Return the scenes of a group that are worth presenting to a user.

        Always contains the named scenes. With ``include_unnamed`` (the
        default) it also contains unnamed scenes that are actually in use -
        i.e. at least one device in the group stores a concrete level for them
        in its scene table. That keeps the list meaningful even when the
        router stores no scene names at all (use ``Scene.display_name`` for a
        generated fallback name), without dumping all ~4000 theoretical
        block/scene combinations on the user.

        Sorted by scene address (block, then scene).
        """
        group_id = int(group_id)
        scenes = [
            scene
            for scene in self.scenes.values()
            if scene.address.group == group_id
            and (
                scene.name is not None
                or (include_unnamed and self._scene_is_in_use(scene))
            )
        ]
        scenes.sort(key=lambda x: (x.address.block, x.address.scene))
        return scenes

    def _scene_is_in_use(self, scene) -> bool:
        """Whether any device in the scene's group has a level for the scene.

        Scene-table entries of "*" mean "ignore scene command", so a scene
        where every group device is "*" (or unknown) is not recallable in any
        useful way and is not considered in use.
        """
        group = self.router.groups.groups.get(scene.address.group)
        if group is None:
            return False

        index = scene.address.to_device_int()
        for device_address in group.devices:
            device = self.router.devices.devices.get(device_address)
            if device is None or not device.is_load or not device.levels:
                continue
            if index >= len(device.levels):
                continue
            level = device.levels[index]
            if level is None or str(level).strip() in ("", "*"):
                continue
            return True
        return False


def parse_scene_names(result):
    """Parse a QUERY_SCENE_NAMES (C:166) reply payload.

    The payload is a list of "@<group>.<block>.<scene>:<name>" entries
    separated by ",@" - splitting on "@" alone leaves a trailing "," on every
    name except the last. Names may themselves contain ":" (so the
    address/name split happens only on the first colon) and ",".

    Returns a dict of SceneAddress -> name. Malformed entries are logged and
    skipped.
    """
    names = {}
    if not result:
        return names

    try:
        parts = result.strip().lstrip("@").split(",@")
    except AttributeError:
        _LOGGER.error(
            "Response result is not a string - cannot parse scene names, no scenes added."
        )
        return names

    for part in parts:
        # Defensive: strip any comma left over from "@"-style splitting.
        part = part.rstrip(",").strip()
        if not part:
            continue
        sub_parts = part.split(":", 1)

        try:
            if len(sub_parts) < 2:
                _LOGGER.warning(f"Invalid scene part format: {part}")
                continue
            scene_address = SceneAddress(*[int(a) for a in sub_parts[0].split(".")])
            name = sub_parts[1].strip()
            if name:
                names[scene_address] = name
        except (KeyError, ValueError, IndexError, TypeError) as e:
            _LOGGER.error(f"Error parsing scene address {part}: {e}")

    return names


async def _query_scene_names(router, group_id=None):
    """Query C:166 (optionally per group) and return the parsed name dict.

    Failures - error replies, timeouts, an unanswered query - are logged and
    yield an empty dict so that scene-name collection can never stall or
    abort the router initialisation.
    """
    parameters = []
    label = "Bare QUERY_SCENE_NAMES (C:166)"
    if group_id is not None:
        parameters = [CommandParameter(CommandParameterType.GROUP, group_id)]
        label = f"QUERY_SCENE_NAMES (C:166) for group {group_id}"

    try:
        response = await router.query(
            Command(CommandType.QUERY_SCENE_NAMES, parameters),
            timeout=SCENE_NAME_QUERY_TIMEOUT,
        )
    except (asyncio.TimeoutError, CommandResponseTimeout):
        _LOGGER.warning(f"{label} was not answered within {SCENE_NAME_QUERY_TIMEOUT}s.")
        return {}

    if response is None or response.command_message_type == MessageType.ERROR:
        _LOGGER.warning(f"{label} failed: {response}")
        return {}

    return parse_scene_names(response.result)


async def get_scenes(router, groups):

    for group in groups.groups.values():
        for block in range(1, 254):
            for scene in range(1, 17):
                scene = Scene(SceneAddress(int(group.group_id), int(block), int(scene)))
                router.scenes.register_scene(scene.address, scene)

    names = {}

    # A bare C:166 is documented as "Query all scene names in group" and on
    # real firmware (e.g. 910 / 4.3.1.0) returns only a subset of the named
    # scenes. Query it anyway, then query per group with a G: parameter and
    # merge the results.
    names.update(await _query_scene_names(router))

    for group in groups.groups.values():
        names.update(await _query_scene_names(router, group.group_id))

    if not names:
        _LOGGER.warning("No scene names returned from router")
        return

    for scene_address, name in names.items():
        router.scenes.update_scene_name(scene_address, name)
