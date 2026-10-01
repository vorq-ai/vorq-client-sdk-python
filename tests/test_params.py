"""Local param validation against the model's published params_schema."""

from __future__ import annotations

import warnings

import pytest

from vorq._params import check_input
from vorq.errors import ValidationError

SCHEMA = {
    "type": "object",
    "properties": {
        "input": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "object"}}]},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
        "n": False, "stream": False, "stream_options": False,
    },
    "additionalProperties": True,
}


def test_valid_input_passes_silently():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        check_input(SCHEMA, {"input": "hi", "temperature": 0.7})


def test_forbidden_key_raises():
    with pytest.raises(ValidationError, match="stream"):
        check_input(SCHEMA, {"input": "hi", "stream": True})


def test_type_violation_raises():
    with pytest.raises(ValidationError):
        check_input(SCHEMA, {"input": "hi", "temperature": "hot"})


def test_unknown_key_warns_but_passes():
    with pytest.warns(UserWarning, match="reasoning_effort"):
        check_input(SCHEMA, {"input": "hi", "reasoning_effort": "high"})


def test_none_schema_is_noop():
    check_input(None, {"anything": object()})


REASONING_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {"type": "string"},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192, "x-vorq-billing": "unit"},
        "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 8192, "x-vorq-billing": "unit"},
        "reasoning_effort": {"enum": ["none", "low", "medium", "high", "xhigh", "max"], "x-vorq-billing": "budget"},
        "reasoning_max_tokens": {"type": "integer", "minimum": 0, "maximum": 8192, "x-vorq-billing": "budget"},
        "min_tokens": {"type": "integer", "minimum": 0, "maximum": 8192, "x-vorq-billing": "budget"},
    },
    "additionalProperties": True,
}


def test_reasoning_budget_within_output_cap_passes():
    check_input(REASONING_SCHEMA, {"input": "hi", "max_tokens": 4096, "reasoning_max_tokens": 2048})


def test_reasoning_budget_at_or_above_output_cap_raises():
    """The whole output budget spent on thinking guarantees an empty answer."""
    with pytest.raises(ValidationError, match="reasoning_max_tokens"):
        check_input(REASONING_SCHEMA, {"input": "hi", "max_tokens": 2048, "reasoning_max_tokens": 2048})


def test_min_tokens_above_output_cap_raises():
    with pytest.raises(ValidationError, match="min_tokens"):
        check_input(REASONING_SCHEMA, {"input": "hi", "max_output_tokens": 100, "min_tokens": 200})


def test_budget_checks_skip_when_no_output_cap_given():
    # No cap in the input: the schema maximum already bounds the budget keys.
    check_input(REASONING_SCHEMA, {"input": "hi", "reasoning_max_tokens": 8000})


def test_reasoning_effort_enum_enforced():
    check_input(REASONING_SCHEMA, {"input": "hi", "reasoning_effort": "none"})
    with pytest.raises(ValidationError, match="reasoning_effort"):
        check_input(REASONING_SCHEMA, {"input": "hi", "reasoning_effort": "minimal"})


def test_declare_units_recognizes_every_output_cap_spelling():
    """All three OpenAI spellings must drive units_out — a recognized cap that
    escrowed the 4096 default would silently clamp the client below what it set."""
    from vorq._client import _declare_units

    assert _declare_units({"input": "hi", "max_tokens": 1000})[1] == 1000
    assert _declare_units({"input": "hi", "max_output_tokens": 2000})[1] == 2000
    assert _declare_units({"input": "hi", "max_completion_tokens": 3000})[1] == 3000
    assert _declare_units({"input": "hi"})[1] == 4096


def test_declare_units_honors_an_explicit_units_out():
    """The caller's declaration wins over every heuristic here.

    The catalog serves no modality, so this SDK cannot tell an embeddings model from a text
    one by looking. An input-metered job declares `units_out=0` itself — and 0 must survive,
    not fall through to the 4096 default, or the client escrows an output leg the job can
    never spend and the whole point of the input-only price is lost.
    """
    from vorq._client import _declare_units

    assert _declare_units({"input": "hi"}, units_out=0)[1] == 0
    # and it overrides a recognized cap rather than deferring to it
    assert _declare_units({"input": "hi", "max_tokens": 1000}, units_out=0)[1] == 0
    assert _declare_units({"input": "hi"}, units_out=77)[1] == 77
    # None is "not stated", which is the existing behaviour
    assert _declare_units({"input": "hi"}, units_out=None)[1] == 4096


def test_declare_units_refuses_a_negative_units_out():
    from vorq._client import _declare_units
    from vorq.errors import ValidationError

    with pytest.raises(ValidationError, match="units_out"):
        _declare_units({"input": "hi"}, units_out=-1)


async def test_submit_validates_against_fetched_schema(no_sleep):
    """submit() pulls /v1/models once and rejects a forbidden param before posting.

    Submissions are sealed-only, so this needs a real signer + cipher (from
    .env.test) to get past the sealed-requirement check and reach validation.
    """
    import httpx

    from tests.conftest import json_response
    from vorq import Client

    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/auth/nonce":
            return json_response(200, {"nonce": "n", "expires_at": 9999999999, "chain_id": 84532})
        if request.url.path == "/auth/session":
            return json_response(200, {"token": "vorq_sess_x", "expires_at": 9999999999})
        if request.url.path == "/v1/models":
            return json_response(200, {"object": "list", "data": [
                {"id": "deepseek-ai/deepseek-v4-pro:fp8", "object": "model", "vorq": {"modality": "text", "params_schema": SCHEMA}},
            ]})
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValidationError):
        await client.submit("deepseek-ai/deepseek-v4-pro:fp8", {"input": "hi", "n": 4}, sla="1h")
    assert "/v1/jobs" not in seen_paths      # rejected locally, nothing posted
    await client.aclose()
