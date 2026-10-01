"""The VORQ exception hierarchy.

Every error the SDK raises descends from :class:`VorqError`. Wire errors are
mapped 1:1 from HTTP status codes and the wire ``error.type``; the SDK never
invents semantics.
"""

from __future__ import annotations

from typing import Any


class VorqError(Exception):
    """Base exception. Carries the wire ``.type``, ``.status_code`` and ``.request_id``.

    ``status_code`` is the HTTP status the error arrived on, or ``None`` when the
    SDK raised it locally without a round trip. The class already implies the
    common statuses, but only four of them — a surface that has to restate an
    error as HTTP (the OpenAI-compat transport) needs the original, so that a
    ``429`` stays a ``429`` and keeps its retry semantics.
    """

    def __init__(
        self,
        message: str,
        *,
        type: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.request_id = request_id
        self.status_code = status_code


class AuthenticationError(VorqError):
    """Bad or missing session token (401)."""


class NotFoundError(VorqError):
    """Unknown job or model id (404)."""


class StateConflictError(VorqError):
    """Illegal state transition (409), e.g. cancelling a terminal job."""


class ValidationError(VorqError):
    """Rejected request (400), e.g. a disallowed param or unknown model."""


class VerificationError(VorqError):
    """A provider's attestation evidence failed client-edge verification."""


class EscrowKeyUnverified(VerificationError):
    """The coordinator's escrow key could not be shown to be what it claims.

    Raised **before** anything is posted, and it is the whole fail-closed
    contract for an open order: an open order's payload is sealed to that key and
    to nothing else, so a key whose evidence does not verify is a key the client
    cannot honestly seal a plaintext to.

    There is deliberately no fallback. Re-targeting the order at a named provider
    is the caller's decision — ``submit(..., provider=N)`` — because an SDK that
    quietly picked one on a verification failure would have turned a fail-closed
    into a fail-quiet, and the caller would never learn that the confidentiality
    it asked for was not the confidentiality it got.
    """


class ResultIntegrityError(VorqError, ValueError):
    """A settled job's result is not readable as a result.

    Raised on three shapes. The bytes stored under the job's ``result_cid`` do
    not decode to a result object — whatever is at that name, it is not an
    answer. Or they decode but the seal does not open, because they were sealed
    to a result key this client does not hold. Or the job reports ``completed``
    and names no ``result_cid`` at all: a settled job names its result, and the
    inline body on the row is a copy the coordinator wrote rather than the one
    the provider settled, so there is nothing to fall back to. All three mean the
    same thing to a caller — this job has no result to hand back — so the SDK
    raises instead of returning a value it would have to invent.

    Also a :class:`ValueError`: the failure is about the value of the bytes, and
    callers that catch either type see it.
    """

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message, type="result_integrity", request_id=request_id)


class WaitTimeout(VorqError):
    """A ``result()`` bound elapsed.

    Carries ``.job_id`` so the caller can persist it and re-attach; the work
    continues on the network, and timing out here cancels nothing.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, type="wait_timeout", request_id=request_id)
        self.job_id = job_id


class JobFailed(VorqError):
    """A job settled as ``failed`` or ``cancelled``.

    Carries ``.error_type``. A failed job names its cause in the one canonical
    vocabulary — ``provider_fail`` or ``reclaim``. A job that was cancelled or
    that expired is not a failure and carries no cause on the job object at all,
    so ``.error_type`` is its status, ``cancelled``.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        job_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, type=error_type, request_id=request_id)
        self.error_type = error_type
        self.job_id = job_id


_STATUS_MAP: dict[int, type[VorqError]] = {
    400: ValidationError,
    401: AuthenticationError,
    404: NotFoundError,
    409: StateConflictError,
}


def error_from_wire(
    status_code: int,
    body: Any,
    *,
    request_id: str | None,
) -> VorqError:
    """Build the mapped exception from a wire error response.

    ``body`` is the parsed JSON envelope ``{"error": {message, type, ...}}``;
    a missing or malformed envelope degrades to a generic message.
    """
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or f"HTTP {status_code}"
        wire_type = error.get("type")
    else:
        message = f"HTTP {status_code}"
        wire_type = None
    cls = _STATUS_MAP.get(status_code, VorqError)
    return cls(message, type=wire_type, request_id=request_id, status_code=status_code)


class BatchFailed(VorqError):
    """A batch ended ``failed``.

    The batch's **input file** was refused — the object could not be resolved, or
    nothing in it could be read as lines. It is not a per-line failure: those are
    rows in the error file and reach a caller as :class:`~vorq._results.JobError`,
    never as this. Carries ``.batch_id`` so the caller can re-read the object and
    see what the coordinator said about the file.
    """

    def __init__(
        self,
        message: str,
        *,
        batch_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, type="batch_failed", request_id=request_id)
        self.batch_id = batch_id
