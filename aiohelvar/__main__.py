"""Console entry point for aiohelvar's read-only tooling.

    python -m aiohelvar diagnose <host> [--port 50000] [--timeout 5] [--json] [-v]
    python -m aiohelvar mock [--profile modern|legacy] [--host 127.0.0.1] [--port 50000] [-v]

`diagnose` runs a read-only capability/version check against a router and prints
a report (exit code 0 if the router looks compatible, 1 otherwise). `mock` starts
a fake router you can point the diagnostics (or Home Assistant) at; send it
SIGHUP to toggle firmware profile.
"""

import argparse
import asyncio
import json
import logging
import sys


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    # Log to stderr so --json output on stdout stays machine-readable.
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from .diagnostics import run_diagnostics

    report = asyncio.run(
        run_diagnostics(args.host, args.port, timeout=args.timeout)
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())

    level, _ = report.verdict()
    return 0 if level == "ok" else 1


def _cmd_mock(args: argparse.Namespace) -> int:
    from .mock_router import MockRouter, BUILTIN_PROFILES

    profile = BUILTIN_PROFILES[args.profile]
    mock = MockRouter(profile=profile, host=args.host, port=args.port)
    try:
        asyncio.run(mock.serve_forever())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\n[mock] stopped.", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aiohelvar",
        description="Read-only HelvarNet diagnostics and a configurable mock router.",
    )
    # Shared options, added to each subcommand so `-v` works after the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (-v for info, -vv for every frame)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser(
        "diagnose",
        parents=[common],
        help="run a read-only connection/version/capability check",
    )
    diag.add_argument("host", help="router hostname or IP address")
    diag.add_argument("-p", "--port", type=int, default=50000, help="TCP port (default 50000)")
    diag.add_argument(
        "-t", "--timeout", type=float, default=5.0, help="per-query timeout in seconds (default 5)"
    )
    diag.add_argument("--json", action="store_true", help="print the report as JSON")
    diag.set_defaults(func=_cmd_diagnose)

    mock = sub.add_parser(
        "mock", parents=[common], help="run a fake router to test against"
    )
    mock.add_argument(
        "--profile",
        choices=["modern", "legacy"],
        default="modern",
        help="firmware behaviour to start with (default modern)",
    )
    mock.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    mock.add_argument("-p", "--port", type=int, default=50000, help="TCP port (default 50000)")
    mock.set_defaults(func=_cmd_mock)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
