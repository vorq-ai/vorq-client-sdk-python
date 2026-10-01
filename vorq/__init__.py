"""``vorq`` — the async Python client for the VORQ inference exchange."""

from __future__ import annotations

from ._client import Client, mint_session_token
from ._crypto import Cipher, SealedBoxCipher, Signer, WalletSigner
from ._handles import JobHandle
from ._openai_compat import SealingTransport, sealing_http_client
from ._results import EmbeddingResult, JobError, MediaResult, TextResult
from .errors import (
    AuthenticationError,
    BatchFailed,
    EscrowKeyUnverified,
    JobFailed,
    NotFoundError,
    ResultIntegrityError,
    StateConflictError,
    ValidationError,
    VerificationError,
    VorqError,
    WaitTimeout,
)
from .verify import Verifier

__all__ = [
    "Client",
    "mint_session_token",
    "sealing_http_client",
    "SealingTransport",
    "JobHandle",
    "TextResult",
    "MediaResult",
    "EmbeddingResult",
    "JobError",
    "Signer",
    "Cipher",
    "WalletSigner",
    "SealedBoxCipher",
    "Verifier",
    "VorqError",
    "AuthenticationError",
    "NotFoundError",
    "StateConflictError",
    "ValidationError",
    "ResultIntegrityError",
    "VerificationError",
    "EscrowKeyUnverified",
    "WaitTimeout",
    "JobFailed",
    "BatchFailed",
]
