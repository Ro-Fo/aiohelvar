"""Tests for DALI-2 energy/diagnostics parsing and querying (C:252/C:253)."""

import pytest

from aiohelvar.dali2 import (
    UNAVAILABLE,
    UNSUPPORTED,
    DALI2NotSupportedError,
    DALI2QueryError,
    DALI2Result,
    parse_dali2_result,
)
from aiohelvar.mock_router import MODERN, FirmwareProfile, MockRouter
from aiohelvar.parser.address import HelvarAddress
from aiohelvar.parser.command_type import CommandType
from aiohelvar.router import Router


class TestParse:
    def test_parses_named_values(self):
        r = parse_dali2_result("ACTE:1.234,ACTP:5.678")
        assert r["ACTE"] == 1.234
        assert r.value("ACTP") == 5.678
        assert set(r.names) == {"ACTE", "ACTP"}

    def test_sentinels(self):
        r = parse_dali2_result("APPP:-1,ACTP:-2,ACTE:10.0")
        # -1 unsupported, -2 unavailable -> value() is None, raw keeps the sentinel
        assert r.raw("APPP") == UNSUPPORTED
        assert r.value("APPP") is None
        assert r.is_supported("APPP") is False
        assert r.status("APPP") == "unsupported"

        assert r.raw("ACTP") == UNAVAILABLE
        assert r.value("ACTP") is None
        assert r.is_available("ACTP") is False
        assert r.status("ACTP") == "unavailable"

        assert r.value("ACTE") == 10.0
        assert r.status("ACTE") == "ok"

    def test_missing_and_empty(self):
        r = parse_dali2_result("")
        assert r.names == []
        assert r.value("ACTE") is None
        assert r.status("ACTE") == "missing"

    def test_malformed_fields_are_skipped(self):
        r = parse_dali2_result("ACTE:1.0,GARBAGE,ACTP:notanumber,APPE:2.0")
        assert r.value("ACTE") == 1.0
        assert r.value("APPE") == 2.0
        assert "GARBAGE" not in r
        assert "ACTP" not in r

    def test_to_dict(self):
        r = parse_dali2_result("ACTE:1.0,APPP:-1")
        assert r.to_dict() == {"ACTE": 1.0, "APPP": None}


@pytest.mark.asyncio
async def test_query_energy_against_mock():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            result = await router.query_dali2_energy(HelvarAddress(1, 2, 3, 4), timeout=2.0)
        finally:
            await router.disconnect()

    assert isinstance(result, DALI2Result)
    assert result.value("ACTE") == 1.234
    assert result.value("ACTP") == 5.678
    # APPP is -1 in the mock payload -> unsupported
    assert result.status("APPP") == "unsupported"
    assert result.value("APPP") is None


@pytest.mark.asyncio
async def test_query_diagnostics_against_mock():
    async with MockRouter(MODERN, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            result = await router.query_dali2_diagnostics(HelvarAddress(1, 2, 3, 4), timeout=2.0)
        finally:
            await router.disconnect()

    assert result.value("LSF") == 0.0
    assert result.status("LSTL") == "unavailable"  # -2 in mock payload


@pytest.mark.asyncio
async def test_query_energy_on_unsupported_hardware_raises_not_supported():
    # Old firmware rejects C:252 with error 15 -> a clear "not supported" error.
    profile = FirmwareProfile(
        name="no-dali2",
        unsupported_commands=frozenset({CommandType.QUERY_DALI2_ENERGY.command_id}),
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            with pytest.raises(DALI2NotSupportedError) as excinfo:
                await router.query_dali2_energy(HelvarAddress(1, 2, 3, 4), timeout=2.0)
            assert excinfo.value.error_code == 15
            assert "not supported" in str(excinfo.value)
        finally:
            await router.disconnect()


@pytest.mark.asyncio
async def test_query_energy_other_error_is_plain_query_error():
    # A non-"unsupported" error (e.g. 11 device does not exist) is a regular
    # DALI2QueryError, not the not-supported subclass.
    profile = FirmwareProfile(
        name="dali2-but-no-device",
        unsupported_commands=frozenset({CommandType.QUERY_DALI2_ENERGY.command_id}),
        error_code=11,
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            with pytest.raises(DALI2QueryError) as excinfo:
                await router.query_dali2_energy(HelvarAddress(1, 2, 3, 4), timeout=2.0)
            assert not isinstance(excinfo.value, DALI2NotSupportedError)
            assert excinfo.value.error_code == 11
        finally:
            await router.disconnect()
