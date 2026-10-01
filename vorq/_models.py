"""The ``models`` namespace."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._client import Client

_CACHE_TTL = 300.0


class Models:
    def __init__(self, client: "Client") -> None:
        self._client = client
        self._cache: list | None = None
        self._cached_at = 0.0

    async def list(self) -> list:
        """Return the model list (``GET /v1/models``).

        Each entry is an OpenAI-shaped model object plus a ``vorq`` capability
        block, passed through unchanged.
        """
        resp = await self._client._request("GET", "/v1/models")
        data = resp.json().get("data", [])
        self._cache, self._cached_at = data, time.monotonic()
        return data

    async def params_schema(self, model: str) -> dict | None:
        """The model's published input schema (``vorq.params_schema``), cached.

        **Nothing serves this field today, and the ``None`` is the design.**
        ``GET /v1/models`` is the coordinator's projection of the chain's model
        table — ``{id, object, owned_by, vorq: {model_id, enabled}}`` — because
        the on-chain registry stores an id, an enabled flag and nothing else (the
        name is emitted by ``registerModel`` and never stored), and the capability
        record the schema would live in is off-chain curation work that is not
        built. **Modality is not on chain either**, which is why a provider
        serving media declares it in its own config: there is nowhere for the
        catalog to have read it from.

        So this returns ``None`` for every model, ``check_input`` validates
        nothing, and a submission goes out unchecked. That is the direction the
        degrade has to run: a stale or invented schema would refuse a param the
        serving provider supports, and the caller cannot fix that from their
        side, while an unvalidated param is stripped by the provider's own
        allowlist — the authoritative strip in either case. **No schema means no
        validation, never wrong validation.**

        The method stays because the read is the whole client-side half of the
        contract: the day a catalog serves the field, this returns it and
        ``submit`` starts refusing before it seals, with no SDK change at all.
        """
        if self._cache is None or time.monotonic() - self._cached_at > _CACHE_TTL:
            await self.list()
        for m in self._cache or []:
            vorq = m.get("vorq") or {}
            if m.get("id") == model or vorq.get("family") == model:
                return vorq.get("params_schema")
        return None
