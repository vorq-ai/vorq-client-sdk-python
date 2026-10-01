"""The chain context and the flat order terms the coordinator's post door reads.

Two things live here and nothing else:

* :class:`ChainContext` — the deployment a signature belongs to. It is read once
  from ``GET /evm/chain`` and cached for the client's life, and it carries **all
  four** contract addresses the node serves. Three of them are addresses this SDK
  itself uses; ``provider_registry`` is not, and it is carried anyway, because the
  two VORQ EIP-712 domains differ in exactly one member — ``verifyingContract`` —
  so a context that dropped it would leave any consumer of this type guessing
  between two separators that share a name and a version. A wrong
  ``verifyingContract`` does not raise: it produces a signature that recovers to a
  stranger, which nothing on the wire can tell from a forgery.
* :class:`OrderTerms` — the nine members of the contract's ``Order`` type, at the
  widths the chain gives them, plus the one translation to the wire's field
  names. **There is no CID member, of any spelling.** The name does not exist
  when the client signs, because the coordinator mints it when it pins; the
  contract stores a ``taskCid`` on the row and does not hash it.

This module opens no socket and reads no configuration. It is the shape of the
thing signed, and the signing itself is ``_crypto.py``'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._money import format_usd
from .errors import ValidationError

#: The registries' EIP-712 domain. Name and version are shared by JobRegistry and
#: ProviderRegistry alike — ``verifyingContract`` is the whole difference.
ORDER_DOMAIN_NAME = "VORQ Jobs"
#: The ProviderRegistry's, which this SDK signs nothing against but can state.
REGISTRY_DOMAIN_NAME = "VORQ Providers"
ORDER_DOMAIN_VERSION = "2"

#: The four addresses ``GET /evm/chain`` serves, in the order this module reads
#: them. All four are required: a context missing one is not a context.
CONTRACT_FIELDS = ("job_registry", "provider_registry", "ask_registry", "usdc")

UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1
UINT128_MAX = 2**128 - 1


def _address(value: object, field: str) -> str:
    """A 20-byte hex address, or a refusal naming the field it came from."""
    if not isinstance(value, str):
        raise ValidationError(
            f"GET /evm/chain: contracts.{field} is not a string", type="invalid_request_error"
        )
    raw = value[2:] if value[:2].lower() == "0x" else value
    if len(raw) != 40:
        raise ValidationError(
            f"GET /evm/chain: contracts.{field} is not a 20-byte address",
            type="invalid_request_error",
        )
    try:
        bytes.fromhex(raw)
    except ValueError as exc:
        raise ValidationError(
            f"GET /evm/chain: contracts.{field} is not hex", type="invalid_request_error"
        ) from exc
    return "0x" + raw


def _uint(value: object, field: str, ceiling: int) -> int:
    """A bounded unsigned integer.

    Booleans are refused explicitly: ``isinstance(True, int)`` is true in
    Python, and a ``True`` that became a ``1`` here would be a rate.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{field} must be an integer in [0, {ceiling}], got {value!r}",
            type="invalid_request_error",
        )
    if value < 0 or value > ceiling:
        raise ValidationError(
            f"{field} must be an integer in [0, {ceiling}], got {value}",
            type="invalid_request_error",
        )
    return value


@dataclass(frozen=True)
class TokenDomain:
    """The payment token's EIP-712 name and version. They differ per network, so the node states them."""

    name: str
    version: str


@dataclass(frozen=True)
class ChainContext:
    """The deployment every signature in this SDK belongs to.

    Frozen on purpose: it is cached for the client's lifetime and handed to every
    signing call, so a mutable copy would let one caller's edit re-target another
    caller's signature at a contract it never meant to authorize.
    """

    chain_id: int
    job_registry: str
    provider_registry: str
    ask_registry: str
    usdc: str
    #: The payment token's own decimals: a USD amount is ``atomic / 10**decimals``.
    decimals: int
    #: The token's EIP-712 name and version, which the payment is signed under.
    token_domain: TokenDomain

    @classmethod
    def from_wire(cls, payload: Any) -> "ChainContext":
        """Parse ``GET /evm/chain``. Every one of the four must be present.

        ``head_block``, ``block_time_ms`` and ``fee_bps`` are on that body too
        and are deliberately not read: this SDK does not wait on a head and
        assembles no transaction (Q19).
        """
        if not isinstance(payload, dict):
            raise ValidationError(
                "GET /evm/chain did not answer an object", type="invalid_request_error"
            )
        contracts = payload.get("contracts")
        if not isinstance(contracts, dict):
            raise ValidationError(
                "GET /evm/chain answered no contracts block", type="invalid_request_error"
            )
        missing = [f for f in CONTRACT_FIELDS if contracts.get(f) is None]
        if missing:
            raise ValidationError(
                "GET /evm/chain is missing contract "
                f"{', '.join(missing)}: this client needs all four",
                type="invalid_request_error",
            )
        # The token's own name and version are signed over, so half of one is
        # worse than none: it produces a signature that recovers to a stranger.
        domain = payload.get("token_domain")
        if (
            not isinstance(domain, dict)
            or not isinstance(domain.get("name"), str) or not domain["name"]
            or not isinstance(domain.get("version"), str) or not domain["version"]
        ):
            raise ValidationError(
                "GET /evm/chain answered no usable token_domain", type="invalid_request_error"
            )
        return cls(
            chain_id=_uint(payload.get("chain_id"), "chain_id", UINT64_MAX),
            decimals=_uint(payload.get("decimals"), "decimals", 36),
            token_domain=TokenDomain(domain["name"], domain["version"]),
            **{f: _address(contracts[f], f) for f in CONTRACT_FIELDS},
        )

    @property
    def contracts(self) -> dict[str, str]:
        """The four addresses, keyed as the node keys them."""
        return {f: getattr(self, f) for f in CONTRACT_FIELDS}


def order_domain(ctx: ChainContext) -> dict[str, Any]:
    """The JobRegistry's EIP-712 domain — ``Order`` and ``Cancel`` both live here."""
    return {
        "name": ORDER_DOMAIN_NAME,
        "version": ORDER_DOMAIN_VERSION,
        "chainId": ctx.chain_id,
        "verifyingContract": ctx.job_registry,
    }


def registry_domain(ctx: ChainContext) -> dict[str, Any]:
    """The ProviderRegistry's EIP-712 domain.

    Nothing in this SDK signs a registry op — that is the provider daemon's half.
    It is derivable here because the address is on the context, and it exists so
    the difference between the two domains is a fact this package can state and
    test rather than a comment. The two now differ in **name as well as address**:
    a context whose two slots resolved to one contract used to produce digests
    indistinguishable from legitimate ones, and no longer can.
    """
    return {
        "name": REGISTRY_DOMAIN_NAME,
        "version": ORDER_DOMAIN_VERSION,
        "chainId": ctx.chain_id,
        "verifyingContract": ctx.provider_registry,
    }


def payment_domain(ctx: ChainContext) -> dict[str, Any]:
    """The payment token's own EIP-712 domain.

    Name and version differ per network, so they are the node's to state rather
    than this module's to assume: a wrong one changes the separator, and the
    signature then verifies nowhere while looking perfectly well formed.
    """
    return {
        "name": ctx.token_domain.name,
        "version": ctx.token_domain.version,
        "chainId": ctx.chain_id,
        "verifyingContract": ctx.usdc,
    }


@dataclass(frozen=True)
class OrderTerms:
    """The nine signed members of an order, at the chain's own widths.

    ``rate_in`` / ``rate_out`` are the **atomic** on-chain rates — token units per
    ``RATE_SCALE`` units of work — because that is what is signed. The wire
    carries them as USD strings (:meth:`to_wire`).

    ``designated`` is ``0`` for an open order and never ``None`` (Q22): zero is
    the contract's sentinel for "any provider", so a null here would be a
    different statement in a type that has no way to make it.
    """

    c: bytes
    model_id: int
    sla_secs: int
    rate_in: int
    rate_out: int
    units_in: int
    units_out: int
    designated: int
    expires_at: int

    def __post_init__(self) -> None:
        if len(self.c) != 32:
            raise ValidationError(
                f"the container commitment is 32 bytes, got {len(self.c)}",
                type="invalid_request_error",
            )
        for field, ceiling in (
            ("model_id", UINT32_MAX), ("sla_secs", UINT32_MAX),
            ("rate_in", UINT128_MAX), ("rate_out", UINT128_MAX),
            ("units_in", UINT32_MAX), ("units_out", UINT32_MAX),
            ("designated", UINT32_MAX), ("expires_at", UINT64_MAX),
        ):
            _uint(getattr(self, field), f"vorq.{field}", ceiling)

    def message(self) -> dict[str, Any]:
        """The typed-data message, keyed by the contract's own member names."""
        return {
            "c": self.c,
            "modelId": self.model_id,
            "slaSecs": self.sla_secs,
            "rateIn": self.rate_in,
            "rateOut": self.rate_out,
            "unitsIn": self.units_in,
            "unitsOut": self.units_out,
            "designated": self.designated,
            "expiresAt": self.expires_at,
        }

    def to_wire(
        self, *, owner: str, job_id: str, signature: str, decimals: int
    ) -> dict[str, Any]:
        """The order's fields on ``POST /v1/jobs``: flat, the body's top level.

        ``owner`` and ``job_id`` ride alongside the signed members rather than
        inside them: the contract derives both — the owner from the recovered
        signature, the id from ``keccak256(owner ‖ c)`` — so signing either would
        be signing a value the chain is going to recompute anyway. Sending them
        is what lets the node disagree with the client loudly, at the door,
        instead of silently on chain.

        The rates go out as canonical USD strings at the payment token's
        ``decimals``; every other integer is a JSON number.
        """
        return {
            "c": "0x" + self.c.hex(),
            "owner": owner,
            "job_id": job_id,
            "model_id": self.model_id,
            "sla_secs": self.sla_secs,
            "rate_in": format_usd(self.rate_in, decimals),
            "rate_out": format_usd(self.rate_out, decimals),
            "units_in": self.units_in,
            "units_out": self.units_out,
            "designated": self.designated,
            "expires_at": self.expires_at,
            "signature": signature,
        }
