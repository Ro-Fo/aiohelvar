"""HelvarNet error codes.

When a router cannot satisfy a request it replies with an *error* message
(message type ``!``) whose result is a numeric error code, e.g.::

    !V:2,C:100,@1.2.1=15#

This module maps those numeric codes to human readable descriptions so that
callers can log and report meaningful messages instead of a bare number.

The codes and their descriptions are taken from the published *HelvarNet
Overview* document ("Error / Diagnostic Messages" section). They are stable
across firmware revisions. If you hit a code that is not listed here, please
double-check against the current HelvarNet documentation and add it.

Reference: HelvarNet Overview, "Error / Diagnostic Messages".

The subset of codes used below is independently corroborated by the Helvar
Designer Release Notes (Appendix 2, HelvarNet C:252/C:253 examples), which show
error 11 for a query to a device that does not exist, error 5 for a query with
no device address, and errors 12/17 for parameter problems - all matching the
descriptions here.
"""

from enum import IntEnum
from typing import Optional


class HelvarErrorCode(IntEnum):
    """Numeric error codes returned in HelvarNet ``!`` (error) messages."""

    SUCCESS = 0
    INVALID_GROUP_INDEX = 1
    INVALID_CLUSTER = 2
    INVALID_ROUTER_INDEX = 3
    INVALID_SUBNET = 4
    INVALID_DEVICE = 5
    INVALID_SUBDEVICE = 6
    INVALID_BLOCK = 7
    INVALID_SCENE = 8
    CLUSTER_DOES_NOT_EXIST = 9
    ROUTER_DOES_NOT_EXIST = 10
    DEVICE_DOES_NOT_EXIST = 11
    PROPERTY_DOES_NOT_EXIST = 12
    INVALID_RAW_MESSAGE_SIZE = 13
    INVALID_MESSAGE_TYPE = 14
    INVALID_MESSAGE_COMMAND = 15
    MISSING_ASCII_TERMINATOR = 16
    MISSING_ASCII_PARAMETER = 17
    INCOMPATIBLE_VERSION = 18


ERROR_CODE_DESCRIPTIONS = {
    HelvarErrorCode.SUCCESS: "Success",
    HelvarErrorCode.INVALID_GROUP_INDEX: "Invalid group index parameter",
    HelvarErrorCode.INVALID_CLUSTER: "Invalid cluster parameter",
    HelvarErrorCode.INVALID_ROUTER_INDEX: "Invalid router index parameter",
    HelvarErrorCode.INVALID_SUBNET: "Invalid subnet parameter",
    HelvarErrorCode.INVALID_DEVICE: "Invalid device parameter",
    HelvarErrorCode.INVALID_SUBDEVICE: "Invalid sub device parameter",
    HelvarErrorCode.INVALID_BLOCK: "Invalid block parameter",
    HelvarErrorCode.INVALID_SCENE: "Invalid scene parameter",
    HelvarErrorCode.CLUSTER_DOES_NOT_EXIST: "Cluster does not exist",
    HelvarErrorCode.ROUTER_DOES_NOT_EXIST: "Router does not exist",
    HelvarErrorCode.DEVICE_DOES_NOT_EXIST: "Device does not exist",
    HelvarErrorCode.PROPERTY_DOES_NOT_EXIST: "Property does not exist",
    HelvarErrorCode.INVALID_RAW_MESSAGE_SIZE: "Invalid RAW message size",
    HelvarErrorCode.INVALID_MESSAGE_TYPE: "Invalid message type",
    HelvarErrorCode.INVALID_MESSAGE_COMMAND: "Invalid message command",
    HelvarErrorCode.MISSING_ASCII_TERMINATOR: "Missing ASCII terminator",
    HelvarErrorCode.MISSING_ASCII_PARAMETER: "Missing ASCII parameter",
    HelvarErrorCode.INCOMPATIBLE_VERSION: "Incompatible version",
}


# Codes that indicate the router did not understand / does not support the
# command itself (as opposed to a bad parameter). An old router firmware that
# does not implement a newer query answers these commands with code 15, which
# is the tell-tale sign of a capability/firmware mismatch rather than a genuine
# addressing mistake.
UNSUPPORTED_COMMAND_CODES = frozenset(
    {
        HelvarErrorCode.INVALID_MESSAGE_TYPE,
        HelvarErrorCode.INVALID_MESSAGE_COMMAND,
        HelvarErrorCode.INCOMPATIBLE_VERSION,
    }
)


def coerce_error_code(code) -> Optional[int]:
    """Return ``code`` as an int, or ``None`` if it can't be parsed."""
    if code is None:
        return None
    try:
        return int(code)
    except (ValueError, TypeError):
        return None


def describe(code) -> str:
    """Return a human readable description for a HelvarNet error code.

    ``code`` may be an int, a numeric string (as received on the wire) or a
    :class:`HelvarErrorCode`. Unknown codes are reported verbatim rather than
    raising, so this is always safe to call for logging.
    """
    numeric = coerce_error_code(code)
    if numeric is None:
        return f"Unknown error code ({code!r})"
    try:
        return ERROR_CODE_DESCRIPTIONS[HelvarErrorCode(numeric)]
    except ValueError:
        return f"Unknown error code ({numeric})"


def is_unsupported_command(code) -> bool:
    """True if ``code`` means the router does not support the command.

    This is the signature of an old firmware that lacks a newer HelvarNet
    query (most commonly error code 15, "Invalid message command").
    """
    numeric = coerce_error_code(code)
    if numeric is None:
        return False
    try:
        return HelvarErrorCode(numeric) in UNSUPPORTED_COMMAND_CODES
    except ValueError:
        return False
