import asyncio
import logging

_LOGGER = logging.getLogger(__name__)


async def guarded(coro, what, logger=None):
    """Await ``coro``, logging failures instead of letting them escape.

    For fire-and-forget tasks (asyncio.create_task) an uncaught exception is
    only reported as "Task exception was never retrieved" long after the
    fact. Wrapping the coroutine keeps one slow or unanswered query (e.g. a
    CommandResponseTimeout) from crashing initialisation while still leaving
    a clear log line. CancelledError is re-raised so shutdown still works.
    """
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as err:  # pylint: disable=broad-except
        (logger or _LOGGER).warning(f"{what} failed: {err!r}")
        return None


def parse_id_list(result):
    """Parse a HelvarNet comma-separated id list reply into a list of ints.

    Replies such as QUERY_CLUSTERS (C:101) and QUERY_ROUTERS (C:102) carry a
    comma-separated list of numeric ids; some firmware prefixes entries with
    '@'. Non-numeric entries are skipped rather than raising.
    """
    if result is None:
        return []
    ids = []
    for part in str(result).split(","):
        part = part.strip().lstrip("@").strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


class Subscribable:
    """Make a class subscribable.

    Used for Devices and Groups

    """

    def __init__(self) -> None:
        self.subscriptions = []

    def add_subscriber(self, func):
        self.subscriptions.append(func)

    def remove_subscriber(self, func):
        if func in self.subscriptions:
            self.subscriptions.remove(func)

    async def update_subscribers(self):
        for sub in self.subscriptions:
            await asyncio.create_task(sub(self))
