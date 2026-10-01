from .conftest import json_response, make_client

MODELS_BODY = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-ai/deepseek-v4-pro:fp8",
            "object": "model",
            "created": 1752000000,
            "owned_by": "vorq",
            "vorq": {
                "modality": "text",
                "family": "deepseek-ai/deepseek-v4-pro",
                "quantization": "fp8",
                "default": False,
                "slas": ["1h", "24h"],
                "units": {"in": "tokens", "out": "tokens"},
                "params": ["input", "max_output_tokens"],
            },
        }
    ],
}


class TestModelsList:
    async def test_returns_the_data_list_with_vorq_blocks(self):
        def handler(request):
            assert request.url.path == "/v1/models"
            return json_response(200, MODELS_BODY)

        client = make_client(handler)
        models = await client.models.list()
        assert isinstance(models, list)
        assert models[0]["id"] == "deepseek-ai/deepseek-v4-pro:fp8"
        assert models[0]["vorq"]["modality"] == "text"
        await client.aclose()


from vorq._params import check_input

#: What `GET /v1/models` actually serves. The coordinator projects the chain's
#: `models` table and nothing more — `vorq-coordinator-node/src/api/routes/
#: catalog.ts` builds `vorq: { model_id, enabled }` — because the on-chain
#: registry stores `id`, `name`, `modality` and `enabled`, and the capability
#: record is off-chain curation work that is not built. Written out here rather
#: than trimmed from MODELS_BODY above, because MODELS_BODY is the shape the SDK
#: is *willing* to read and this is the shape it is *given*.
COORDINATOR_MODELS_BODY = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-ai/deepseek-v4-pro:fp8",
            "object": "model",
            "owned_by": "vorq",
            "vorq": {"model_id": 1, "enabled": True},
        }
    ],
    "as_of_block": 4242,
}


class TestParamsSchemaFailsSafe:
    """No schema means no validation, and never wrong validation.

    `Models.params_schema` reads a field no route serves. That is not a bug to
    be routed around: a stale or invented schema would refuse a param the
    serving provider supports, which costs a caller a request they cannot fix
    from their side, while an unvalidated param is stripped by the provider's own
    allowlist, which is the authoritative strip either way. So the direction of
    the degrade is the deliberate one, and these tests are what stop a future
    edit from reversing it — a `params_schema` that raised, or defaulted to some
    built-in schema, would turn a harmless absence into a refusal.
    """

    async def test_the_shipped_catalog_carries_no_schema_so_none_comes_back(self):
        # Both branches of the lookup at once: a model the catalog lists, and one
        # it does not. Neither is an error — the absence is the answer.
        def handler(request):
            assert request.url.path == "/v1/models"
            return json_response(200, COORDINATOR_MODELS_BODY)

        client = make_client(handler)
        assert await client.models.params_schema("deepseek-ai/deepseek-v4-pro:fp8") is None
        assert await client.models.params_schema("some/model-nobody-registered:fp8") is None
        await client.aclose()

    def test_no_schema_validates_nothing_rather_than_validating_wrongly(self):
        # Every one of these would be refused against the schema the never-built
        # catalog would have served. Without one they pass, silently, and the
        # serving provider strips what it does not serve. This is the direction
        # of the degrade, asserted at the function that decides it.
        check_input(None, {"input": "hi", "n": 4, "stream": True, "temperature": 99})
