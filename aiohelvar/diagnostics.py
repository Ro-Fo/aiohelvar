"""Read-only HelvarNet router diagnostics.

This module connects to a router and runs a short sequence of **read-only**
queries (never any command that changes device or scene state), reporting for
each one whether it succeeded, returned an error, or timed out. It is designed
to answer, in a few seconds, questions like:

* Is the router reachable on this host/port?
* What firmware / HelvarNet version does it report?
* Does this firmware support device discovery (query C:100)? Old firmware
  answers unsupported queries with error code 15 ("Invalid message command"),
  which is exactly the situation that makes the integration appear to hang.

Every query is bounded by a timeout, so the diagnostics always finish instead
of blocking on the default 30-second command timeout.

Typical use::

    from aiohelvar.diagnostics import run_diagnostics
    report = await run_diagnostics("192.0.2.10")
    print(report.to_text())

or from the console::

    python -m aiohelvar diagnose 192.0.2.10
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .error_codes import coerce_error_code, describe, is_unsupported_command
from .parser.address import HelvarAddress
from .parser.command import Command
from .parser.command_type import CommandType, MessageType
from .router import Router

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 50000
DEFAULT_TIMEOUT = 5.0


class ProbeStatus(Enum):
    """Outcome of a single read-only probe."""

    OK = "OK"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"

    def __str__(self):
        return self.value


@dataclass
class ProbeResult:
    """The result of probing the router with one read-only query."""

    label: str
    command_id: int
    status: ProbeStatus
    detail: str = ""
    result: Optional[str] = None
    error_code: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "command_id": self.command_id,
            "status": str(self.status),
            "detail": self.detail,
            "result": self.result,
            "error_code": self.error_code,
        }


# Command ids of the read-only queries this module knows about. Kept as module
# constants so the report helpers and the tests can refer to them by name.
CMD_WORKGROUP = CommandType.QUERY_WORKGROUP_NAME.command_id  # 107
CMD_ROUTER_VERSION = CommandType.QUERY_ROUTER_VERSION.command_id  # 190
CMD_HELVARNET_VERSION = CommandType.QUERY_HELVARNET_VERSION.command_id  # 191
CMD_CLUSTERS = CommandType.QUERY_CLUSTERS.command_id  # 101
CMD_ROUTERS = CommandType.QUERY_ROUTERS.command_id  # 102
CMD_GROUPS = CommandType.QUERY_GROUPS.command_id  # 165
CMD_DEVICE_DISCOVERY = CommandType.QUERY_DEVICE_TYPES_AND_ADDRESSES.command_id  # 100


@dataclass
class DiagnosticsReport:
    """A structured, human-readable summary of a diagnostics run."""

    host: str
    port: int
    reachable: bool = False
    connect_error: Optional[str] = None
    probes: List[ProbeResult] = field(default_factory=list)

    def get(self, command_id: int) -> Optional[ProbeResult]:
        for probe in self.probes:
            if probe.command_id == command_id:
                return probe
        return None

    def _result_if_ok(self, command_id: int) -> Optional[str]:
        probe = self.get(command_id)
        if probe is not None and probe.status == ProbeStatus.OK:
            return probe.result
        return None

    def supports(self, command_id: int) -> Optional[bool]:
        """Whether the firmware supports a command.

        Returns True if the router replied or returned an error *other* than an
        "unsupported command" code (i.e. the command exists but perhaps the
        parameters/address were wrong), False if it returned code 15
        ("Invalid message command"), and None if it was not probed or timed out.
        """
        probe = self.get(command_id)
        if probe is None:
            return None
        if probe.status == ProbeStatus.OK:
            return True
        if probe.status == ProbeStatus.ERROR:
            return not is_unsupported_command(probe.error_code)
        return None

    @property
    def workgroup_name(self) -> Optional[str]:
        return self._result_if_ok(CMD_WORKGROUP)

    @property
    def router_version(self) -> Optional[str]:
        return self._result_if_ok(CMD_ROUTER_VERSION)

    @property
    def helvarnet_version(self) -> Optional[str]:
        return self._result_if_ok(CMD_HELVARNET_VERSION)

    @property
    def clusters(self) -> Optional[str]:
        return self._result_if_ok(CMD_CLUSTERS)

    @property
    def routers(self) -> Optional[str]:
        return self._result_if_ok(CMD_ROUTERS)

    @property
    def supports_device_discovery(self) -> Optional[bool]:
        return self.supports(CMD_DEVICE_DISCOVERY)

    @property
    def supports_groups(self) -> Optional[bool]:
        return self.supports(CMD_GROUPS)

    def verdict(self) -> Tuple[str, str]:
        """Return an overall ``(level, message)`` where level is ok/warning/error."""
        if not self.reachable:
            detail = f" ({self.connect_error})" if self.connect_error else ""
            return (
                "error",
                f"Could not open a TCP connection to {self.host}:{self.port}{detail}. "
                "Check the host/port and that HelvarNet/TCP is enabled on the router.",
            )

        discovery = self.get(CMD_DEVICE_DISCOVERY)
        if discovery is not None and discovery.status == ProbeStatus.ERROR and (
            is_unsupported_command(discovery.error_code)
        ):
            return (
                "warning",
                "Router reachable, but device discovery (query C:100) is not supported "
                f"by this firmware (error {discovery.error_code}: "
                f"{describe(discovery.error_code)}). The Home Assistant integration "
                "relies on device discovery to enumerate devices, so it will not find "
                "any lights on this firmware. This is a firmware capability limit, not "
                "a wiring or network problem.",
            )
        if self.supports_device_discovery is None:
            return (
                "warning",
                "Router reachable, but device discovery (query C:100) did not return a "
                "response within the timeout. The router may be slow, busy, or replying "
                "in an unexpected format.",
            )
        if discovery is not None and discovery.status == ProbeStatus.ERROR:
            return (
                "warning",
                "Router reachable and it understands device discovery, but the probe "
                f"returned error {discovery.error_code}: {describe(discovery.error_code)}. "
                "This usually means the probed cluster/router address does not match "
                "this router — see the integration's cluster/router settings.",
            )
        return (
            "ok",
            "Router reachable and responds to device discovery. This router looks "
            "compatible with the integration.",
        )

    def to_dict(self) -> Dict[str, Any]:
        level, message = self.verdict()
        return {
            "host": self.host,
            "port": self.port,
            "reachable": self.reachable,
            "connect_error": self.connect_error,
            "workgroup_name": self.workgroup_name,
            "router_version": self.router_version,
            "helvarnet_version": self.helvarnet_version,
            "clusters": self.clusters,
            "routers": self.routers,
            "supports_device_discovery": self.supports_device_discovery,
            "supports_groups": self.supports_groups,
            "verdict": {"level": level, "message": message},
            "probes": [probe.to_dict() for probe in self.probes],
        }

    def to_text(self) -> str:
        lines = []
        title = f"HelvarNet diagnostics for {self.host}:{self.port}"
        lines.append(title)
        lines.append("=" * len(title))
        lines.append(f"{'TCP connection':<20}: {'OK' if self.reachable else 'FAILED'}")

        if self.reachable:
            for probe in self.probes:
                if probe.status == ProbeStatus.OK:
                    suffix = f"-> {probe.result}"
                else:
                    suffix = f"-> {probe.detail}" if probe.detail else ""
                lines.append(f"{probe.label:<20}: {str(probe.status):<7} {suffix}".rstrip())
        elif self.connect_error:
            lines.append(f"{'Error':<20}: {self.connect_error}")

        level, message = self.verdict()
        lines.append("")
        lines.append(f"Verdict: {level.upper()} - {message}")
        return "\n".join(lines)


# (label, command_id, needs_address) describing the read-only probe sequence.
_PROBE_PLAN = [
    ("Workgroup name", CMD_WORKGROUP, False),
    ("Router version", CMD_ROUTER_VERSION, False),
    ("HelvarNet version", CMD_HELVARNET_VERSION, False),
    ("Clusters", CMD_CLUSTERS, False),
    ("Routers", CMD_ROUTERS, False),
    ("Groups", CMD_GROUPS, False),
    ("Device discovery", CMD_DEVICE_DISCOVERY, True),
]


def _discovery_address(router: Router) -> HelvarAddress:
    """Build a syntactically valid @cluster.router.subnet address to probe with.

    The exact address does not matter for a capability probe: we only need to
    tell "command not supported" (error 15) apart from "command understood"
    (a reply or any other error). Values are clamped to valid ranges so we never
    raise while constructing the address.
    """
    cluster = router.cluster_id if 0 <= router.cluster_id <= 253 else 0
    router_id = router.router_id if 1 <= router.router_id <= 254 else 1
    return HelvarAddress(cluster, router_id, 1)


async def _probe(
    router: Router, label: str, command: Command, timeout: float
) -> ProbeResult:
    command_id = command.command_type.command_id
    _LOGGER.debug("Probing %s (C:%s)...", label, command_id)
    try:
        response = await router.query(command, timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.debug("Probe %s timed out after %ss", label, timeout)
        return ProbeResult(
            label, command_id, ProbeStatus.TIMEOUT, f"no response within {timeout}s"
        )
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("Probe %s raised %r", label, err)
        return ProbeResult(label, command_id, ProbeStatus.ERROR, f"exception: {err}")

    if response is None:
        return ProbeResult(label, command_id, ProbeStatus.ERROR, "no response object")

    if response.command_message_type == MessageType.ERROR:
        code = coerce_error_code(response.result)
        detail = f"error {response.result}: {describe(response.result)}"
        _LOGGER.debug("Probe %s -> %s", label, detail)
        return ProbeResult(label, command_id, ProbeStatus.ERROR, detail, response.result, code)

    _LOGGER.debug("Probe %s -> OK (%s)", label, response.result)
    return ProbeResult(
        label, command_id, ProbeStatus.OK, str(response.result), response.result
    )


async def run_diagnostics(
    host: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: Optional[float] = None,
) -> DiagnosticsReport:
    """Run the read-only diagnostics against a router and return a report.

    Never sends any state-changing command. Every step is bounded by a timeout,
    so this always returns rather than hanging, even against firmware that leaves
    unsupported queries unanswered.
    """
    report = DiagnosticsReport(host=host, port=port)
    router = Router(host, port)

    try:
        await asyncio.wait_for(router.open(), connect_timeout or timeout)
    except (OSError, asyncio.TimeoutError) as err:
        report.reachable = False
        report.connect_error = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
        _LOGGER.info("Could not connect to %s:%s - %s", host, port, report.connect_error)
        return report

    report.reachable = True
    try:
        for label, command_id, needs_address in _PROBE_PLAN:
            command_type = CommandType.get_by_command_id(command_id)
            address = _discovery_address(router) if needs_address else None
            command = Command(command_type, command_address=address)
            report.probes.append(await _probe(router, label, command, timeout))
    finally:
        try:
            await router.disconnect()
        except Exception as err:  # pragma: no cover - best-effort cleanup
            _LOGGER.debug("Error during diagnostics disconnect: %r", err)

    return report
