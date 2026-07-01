"""Tests for DALI-2 energy/diagnostics parsing and querying (C:252/C:253)."""

import pytest

from aiohelvar.dali2 import (
    UNAVAILABLE,
    UNSUPPORTED,
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
async def test_query_energy_raises_on_error_reply():
    # A firmware that rejects C:252 returns error 15; the query surfaces it.
    profile = FirmwareProfile(
        name="no-dali2",
        unsupported_commands=frozenset({CommandType.QUERY_DALI2_ENERGY.command_id}),
    )
    async with MockRouter(profile, port=0) as mock:
        router = Router(mock.host, mock.port)
        await router.open()
        try:
            with pytest.raises(DALI2QueryError) as excinfo:
                await router.query_dali2_energy(HelvarAddress(1, 2, 3, 4), timeout=2.0)
            assert excinfo.value.error_code == 15
        finally:
            await router.disconnect()
