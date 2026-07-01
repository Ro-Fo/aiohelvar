"""DALI-2 energy reporting (C:252) and diagnostics/maintenance (C:253).

These HelvarNet queries read the DALI-2 memory banks defined by IEC 62386-252
(energy) and IEC 62386-253 (diagnostics), as documented in the Helvar Designer
Release Notes (Appendix 2). A reply carries a comma-separated list of
``NAME:value`` pairs, e.g.::

    ?V:2,C:252,@1.2.3.4,A:1=ACTE:1.234,ACTP:5.678,APPP:-1#

Two sentinel values can appear in place of a real reading:

* ``-1`` - the device does not support that memory bank or value.
* ``-2`` - the value is temporarily unavailable.

This module parses that payload into a :class:`DALI2Result` and offers helpers
to query it. Parsing the payload is fully specified; **sending** the request is
best-effort (the request wire-format, e.g. the exact address form and whether
``A:1`` is required, should be confirmed against real hardware - every query is
timeout-bounded, so a mismatch fails cleanly rather than hanging).

**Hardware support.** These are DALI-2 features on newer routers (e.g. the 950
with DALI-2 Type 51/52 devices). Older routers (905/910/920) and old firmware do
not implement C:252/C:253 and answer with error 15 ("Invalid message command").
The query helpers guard against this: on such hardware they raise
:class:`DALI2NotSupportedError` immediately instead of returning bogus data, so
calling them on an unsupported router fails loudly and clearly.
"""

import logging
from typing import Dict, List, Optional, Tuple

from .error_codes import coerce_error_code, describe, is_unsupported_command
from .parser.command import Command
from .parser.command_parameter import CommandParameter, CommandParameterType
from .parser.command_type import CommandType, MessageType

_LOGGER = logging.getLogger(__name__)

# Sentinel values that can appear instead of a reading.
UNSUPPORTED = -1.0
UNAVAILABLE = -2.0

# C:252 energy fields: name -> (description, unit). From Designer Release Notes
# Appendix 2 (banks 202/203/204).
ENERGY_FIELDS: Dict[str, Tuple[str, str]] = {
    "ACTE": ("Active Energy", "Wh"),
    "ACTP": ("Active Power", "W"),
    "APPE": ("Apparent Energy", "VAh"),
    "APPP": ("Apparent Power", "VA"),
    "ACTEL": ("Active Energy Loadside", "Wh"),
    "ACTPL": ("Active Power Loadside", "W"),
}


class DALI2QueryError(Exception):
    """Raised when a DALI-2 query returns a HelvarNet error reply."""

    def __init__(self, address, error_code):
        self.address = address
        self.error_code = coerce_error_code(error_code)
        super().__init__(
            f"DALI-2 query to {address} failed: error {error_code} "
            f"({describe(error_code)})"
        )


class DALI2NotSupportedError(DALI2QueryError):
    """Raised when the router/firmware doesn't support DALI-2 queries at all.

    This is the "wrong hardware" case: the router answered C:252/C:253 with an
    "unsupported command" error (typically 15). DALI-2 energy/diagnostics needs a
    newer router such as the 950; older routers (905/910/920) do not implement
    it.
    """

    def __init__(self, address, error_code):
        # Reuse the base formatting but make the cause explicit.
        super().__init__(address, error_code)
        self.args = (
            f"DALI-2 energy/diagnostics (C:252/C:253) is not supported by this "
            f"router/firmware (error {error_code}: {describe(error_code)}). This "
            f"is a DALI-2 feature on newer routers such as the 950; older routers "
            f"(905/910/920) do not implement it.",
        )


class DALI2Result:
    """Parsed ``NAME:value`` payload from a C:252/C:253 reply.

    Access raw values with ``result[name]`` / ``result.raw(name)``. Use
    ``value(name)`` to get the reading with the -1/-2 sentinels turned into
    ``None``, and ``status(name)`` for a word describing the state.
    """

    def __init__(self, values: Dict[str, float]):
        self._values = values

    @property
    def names(self) -> List[str]:
        return list(self._values.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._values

    def __getitem__(self, name: str) -> float:
        return self._values[name]

    def items(self):
        return self._values.items()

    def raw(self, name: str) -> Optional[float]:
        """Raw value including sentinels, or None if the field is absent."""
        return self._values.get(name)

    def is_supported(self, name: str) -> bool:
        return name in self._values and self._values[name] != UNSUPPORTED

    def is_available(self, name: str) -> bool:
        return name in self._values and self._values[name] != UNAVAILABLE

    def value(self, name: str) -> Optional[float]:
        """The reading, or None if absent/unsupported/unavailable."""
        raw = self._values.get(name)
        if raw is None or raw in (UNSUPPORTED, UNAVAILABLE):
            return None
        return raw

    def status(self, name: str) -> str:
        raw = self._values.get(name)
        if raw is None:
            return "missing"
        if raw == UNSUPPORTED:
            return "unsupported"
        if raw == UNAVAILABLE:
            return "unavailable"
        return "ok"

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {name: self.value(name) for name in self._values}

    def __repr__(self):
        return f"DALI2Result({self._values!r})"


def parse_dali2_result(result: Optional[str]) -> DALI2Result:
    """Parse a ``NAME:value,NAME:value`` payload into a :class:`DALI2Result`.

    Unparseable pairs are skipped (and logged), so a malformed field never
    breaks the whole reply.
    """
    values: Dict[str, float] = {}
    if not result:
        return DALI2Result(values)
    for pair in result.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, raw = pair.partition(":")
        name = name.strip()
        try:
            values[name] = float(raw.strip())
        except (ValueError, TypeError):
            _LOGGER.debug("Skipping unparseable DALI-2 field: %r", pair)
    return DALI2Result(values)


async def _query(router, command_type: CommandType, address, timeout) -> DALI2Result:
    # Best-effort request: no value names => the router returns all values.
    command = Command(
        command_type,
        [CommandParameter(CommandParameterType.ACK, "1")],
        command_address=address,
    )
    response = await router.query(command, timeout=timeout)
    if response is None:
        raise DALI2QueryError(address, None)
    if response.command_message_type == MessageType.ERROR:
        # "Unsupported command" (error 15) => wrong hardware/firmware: refuse
        # loudly rather than pretending a DALI-2 read is possible here.
        if is_unsupported_command(response.result):
            raise DALI2NotSupportedError(address, response.result)
        raise DALI2QueryError(address, response.result)
    return parse_dali2_result(response.result)


async def query_energy(router, address, timeout=None) -> DALI2Result:
    """Query DALI-2 energy reporting (C:252) for a device address."""
    return await _query(router, CommandType.QUERY_DALI2_ENERGY, address, timeout)


async def query_diagnostics(router, address, timeout=None) -> DALI2Result:
    """Query DALI-2 diagnostics & maintenance (C:253) for a device address."""
    return await _query(router, CommandType.QUERY_DALI2_DIAGNOSTICS, address, timeout)
