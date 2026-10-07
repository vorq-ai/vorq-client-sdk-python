"""The USD ↔ atomic conversion, held to the shared ``money-v1`` vectors."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from vorq._money import format_usd, parse_usd, usd_arg, usd_atomic, wire_usd
from vorq.errors import ValidationError, VorqError

VECTORS = json.loads((Path(__file__).parent / "vectors" / "money-v1.json").read_text())


def test_the_vectors_are_the_money_format():
    assert VECTORS["format"] == "vorq-money-v1"


@pytest.mark.parametrize("v", VECTORS["canonical"], ids=lambda v: f"{v['usd']}@{v['decimals']}")
def test_canonical_round_trips(v):
    assert parse_usd(v["usd"], v["decimals"]) == int(v["atomic"])
    assert format_usd(int(v["atomic"]), v["decimals"]) == v["usd"]


@pytest.mark.parametrize("v", VECTORS["parse_only"], ids=lambda v: f"{v['usd']}@{v['decimals']}")
def test_parse_only_accepts_trailing_zeros_and_formats_without_them(v):
    assert parse_usd(v["usd"], v["decimals"]) == int(v["atomic"])
    assert format_usd(int(v["atomic"]), v["decimals"]) == v["formats_as"]


@pytest.mark.parametrize("v", VECTORS["refused"], ids=lambda v: v["why"])
def test_refused(v):
    with pytest.raises(ValueError):
        parse_usd(v["usd"], v["decimals"])


def test_a_rate_is_usd_per_million_units():
    assert parse_usd("0.05", 6) == 50_000


@pytest.mark.parametrize("value", [50_000, 0.05, True])
def test_a_caller_amount_that_is_not_str_or_decimal_is_refused_naming_the_unit(value):
    with pytest.raises(ValidationError, match=r'USD per 1M units, e\.g\. "0\.05"'):
        usd_arg(value, "rate_in")


def test_a_decimal_caller_amount_converts_exactly():
    assert usd_atomic(Decimal("0.05"), "rate_in", 6) == 50_000
    assert usd_atomic(Decimal("1E+2"), "rate_in", 6) == 100_000_000
    assert usd_atomic(None, "rate_in", 6) is None


def test_a_caller_amount_finer_than_the_token_is_refused_not_rounded():
    with pytest.raises(ValidationError):
        usd_atomic("0.0000001", "rate_in", 6)


@pytest.mark.parametrize("value", ["0.050", "1e3", 50_000, None, ".5"])
def test_wire_money_must_be_a_canonical_usd_string(value):
    with pytest.raises(VorqError) as excinfo:
        wire_usd(value, "rate_in")
    assert excinfo.value.type == "api_error"


def test_wire_money_reads_as_decimal():
    assert wire_usd("0.05", "rate_in") == Decimal("0.05")
