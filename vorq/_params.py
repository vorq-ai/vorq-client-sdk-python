"""Local validation of a model input against its published ``params_schema``.

The schema comes from ``GET /v1/models`` (``vorq.params_schema``). Validation is
the client-side half of the network's param contract: a known key with an
invalid value, or a param the network forbids, raises before anything is sealed
or paid for; an unknown key warns and passes through (the serving provider
strips what it does not serve).
"""

from __future__ import annotations

import warnings

import jsonschema

from .errors import ValidationError


def check_input(schema: dict | None, input: dict) -> None:
    if not schema or not isinstance(input, dict):
        return
    properties = schema.get("properties") or {}
    # A `false` subschema means the network forbids the key outright. jsonschema's
    # own error for this carries an empty `.path` (the failure is reported against
    # the whole instance, not the property), so resolve forbidden keys directly
    # here rather than relying on the error's path to name them.
    for key, subschema in properties.items():
        if subschema is False and key in input:
            raise ValidationError(
                f"parameter {key!r} is not supported on VORQ", type="invalid_request_error"
            )
    validator = jsonschema.Draft202012Validator(schema)
    for err in validator.iter_errors(input):
        key = err.path[0] if err.path else None
        if err.schema is False:
            raise ValidationError(
                f"parameter {key!r} is not supported on VORQ", type="invalid_request_error"
            )
        raise ValidationError(
            f"parameter {key!r} is invalid: {err.message}", type="invalid_request_error"
        )
    _check_budget_fits_cap(input)
    known = set(properties.keys())
    unknown = sorted(k for k in input if k not in known)
    if unknown:
        warnings.warn(
            f"params not in the model's schema (a provider may ignore them): {', '.join(unknown)}",
            UserWarning,
            stacklevel=3,
        )


# The output-cap spellings, any of which drives the declared (and escrowed) units_out.
_OUTPUT_CAP_KEYS = ("max_tokens", "max_output_tokens", "max_completion_tokens")


def _check_budget_fits_cap(input: dict) -> None:
    """Cross-field rules a per-property schema cannot express.

    Budget-class params spend ``units_out`` from the inside, so they must leave
    room in the cap the same request sets: a reasoning budget equal to the whole
    output budget guarantees an empty answer at full price.
    """
    caps = [input[k] for k in _OUTPUT_CAP_KEYS if isinstance(input.get(k), int)]
    if not caps:
        return  # no cap in this input; the schema's own maximum bounds the budget keys
    cap = min(caps)
    reasoning = input.get("reasoning_max_tokens")
    if isinstance(reasoning, int) and reasoning >= cap:
        raise ValidationError(
            f"reasoning_max_tokens ({reasoning}) must be below the output cap ({cap}): "
            "the whole budget spent on reasoning leaves no room for the answer",
            type="invalid_request_error",
        )
    min_tokens = input.get("min_tokens")
    if isinstance(min_tokens, int) and min_tokens > cap:
        raise ValidationError(
            f"min_tokens ({min_tokens}) exceeds the output cap ({cap})",
            type="invalid_request_error",
        )
