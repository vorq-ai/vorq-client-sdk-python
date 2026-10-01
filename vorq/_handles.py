"""``JobHandle`` — the view you poll, wait on, cancel, and read a result from.

The job on the network is the durable thing; a handle is only a view, so it
carries no state a rerun couldn't rebuild from the job id.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Union

from ._results import EmbeddingResult, MediaResult, TextResult, result_from_raw
from ._sla import poll_interval, sla_seconds, window_from_seconds
from .errors import JobFailed, ResultIntegrityError, ValidationError, WaitTimeout

if TYPE_CHECKING:
    import httpx

    from ._client import Client
    from ._crypto import Cipher

_TERMINAL = {"completed", "failed", "cancelled"}

#: ``JobView.endedBecause`` → the one cause vocabulary, straight from the
#: contract's own constants (``vorq-evm-contracts/src/Types.sol``).
#:
#: **The job row carries no ``error`` object.** ``clientJob`` folds four causes
#: into two statuses — 3 and 4 become ``failed``, 2 and 5 become ``cancelled`` —
#: and passes the cause itself through under ``vorq.ended_because``. A reader
#: that looks for ``job["error"]`` finds nothing and reports the status as the
#: cause, which makes ``provider_fail`` and ``reclaim`` — the two a caller is
#: told it can branch on — unreachable, with no error to say so.
#:
#: 0 (none) and 1 (settled) are absent deliberately: neither reaches a failure.
_ENDED_BECAUSE = {
    2: "cancelled",       # the owner's own signed cancel
    3: "provider_fail",   # the claimant reported it could not deliver
    4: "reclaim",         # claimed, and never settled inside the window
    5: "expired",         # nobody ever claimed it
}


def _end_cause(job: dict) -> str | None:
    """The job's end cause from ``vorq.ended_because``, or ``None`` if unnamed.

    A code this SDK does not know degrades to ``None`` and the caller sees the
    status instead — guessing at an unfamiliar constant would be worse than
    saying less.
    """
    try:
        code = int(str((job.get("vorq") or {}).get("ended_because")))
    except (TypeError, ValueError):
        return None
    return _ENDED_BECAUSE.get(code)


class JobHandle:
    def __init__(
        self,
        client: "Client",
        id: str,
        *,
        sla: str | None = None,
        job: dict | None = None,
        cipher: "Cipher | None" = None,
        task_cid: str | None = None,
    ) -> None:
        self._client = client
        self.id = id
        self._sla = sla
        self._job = job
        self._cipher = cipher if cipher is not None else getattr(client, "cipher", None)
        #: The name the coordinator's storage service minted for this job's
        #: container, read off the submission's answer. The client cannot compute
        #: or predict it — the service names the content, not the client — so this
        #: is the only place it comes from until the post is indexed.
        self.task_cid = task_cid

    async def _fetch(self) -> dict:
        """One read of the job: ``GET /v1/jobs/{id}`` on the coordinator.

        The window is learned from ``vorq.sla_secs``, which is what the row
        carries — an integer, projected from the chain's ``slaSecs``. A row that
        names no usable one leaves ``_sla`` at ``None`` and :meth:`result` falls
        back to its default pacing.
        """
        resp = await self._client._request("GET", f"/v1/jobs/{self.id}")
        job = resp.json()
        self._job = job
        if self._sla is None:
            self._sla = window_from_seconds((job.get("vorq") or {}).get("sla_secs"))
        return job

    async def status(self) -> str:
        """One ``GET /v1/jobs/{id}``; returns the current wire status string."""
        job = await self._fetch()
        return job["status"]

    async def result(self, timeout: float | None = None) -> Union[TextResult, MediaResult, EmbeddingResult]:
        """Poll until terminal, then return the result (or raise ``JobFailed``).

        Paced by the job's SLA — ``sla_seconds/60``, held between 2 s and
        :data:`~vorq._sla.MAX_POLL_INTERVAL_SECONDS` (60 s). The upper bound is
        what stops a ``"24h"`` job being read once every twenty-four minutes,
        which put the last sleep of the loop past the deadline below.
        ``timeout`` defaults to the job's SLA; on expiry raises
        :class:`~vorq.errors.WaitTimeout` carrying ``.job_id``.
        """
        job = await self._fetch()
        sla = self._sla or "1h"
        if timeout is None:
            timeout = sla_seconds(sla)
        interval = poll_interval(sla)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while job["status"] not in _TERMINAL:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise WaitTimeout(
                    f"Job {self.id} did not settle within {timeout}s.", job_id=self.id
                )
            await asyncio.sleep(min(interval, remaining))
            job = await self._fetch()
        if job["status"] == "completed":
            return await self._settled_result(job)
        # The cause comes off `vorq.ended_because`, because that is where the row
        # puts it — there is no `error` object on a job row at all.
        cause = _end_cause(job)
        detail = f" ({cause})" if cause and cause != job["status"] else ""
        raise JobFailed(
            f"Job {self.id} {job['status']}{detail}.",
            error_type=cause or job["status"],
            job_id=self.id,
        )

    async def _settled_result(self, job: dict) -> Union[TextResult, MediaResult, EmbeddingResult]:
        """Read a completed job's result — the one place that decides how.

        A settled job names its result: the bytes are fetched by ``result_cid``
        and opened with the job's cipher.

        There is no inline fallback. A job that reports ``completed`` but names
        no result named nothing to fetch — whatever ``output`` the coordinator
        put on the row is a body it wrote itself, not the one the provider
        settled. That is a broken settle, so it raises rather than returning
        somebody else's copy (or an empty result).
        """
        cid = job.get("result_cid")
        if not cid:
            raise ResultIntegrityError(
                f"Job {self.id} is completed but names no result "
                "(result_cid is null) — it settled without a named result, so "
                "there is nothing this client can fetch or open."
            )
        raw = await self._client.fetch_blob(cid)
        return result_from_raw(raw, job, cipher=self._cipher)

    async def _cancel_request(self) -> "httpx.Response":
        """Sign ``Cancel(jobId, issuedAt)`` and relay it. Returns the node's answer.

        The cancel is a **chain op the node relays**, not a state change the node
        decides. ``JobRegistry.cancel(jobId, issuedAt, signature)`` never reads
        ``msg.sender`` and there is no ``cancelFor``, so this signature is the
        entire authority over ending the job — which is exactly why it is made
        here and not there. A relaying node that could author one could cancel
        any job that ever passed through it.

        ``issued_at`` is unix seconds, stamped now and sent on the body: the
        chain bounds it to ±600 s of landing, so it has to be the value that was
        signed rather than one the relay picks. There is no nonce — a cancelled
        job cannot be cancelled twice, so the job's one-shot state machine is the
        replay guard.

        A client with no wallet cannot author this at all, and it says so before
        the request rather than letting the node answer ``400`` to a body with no
        signature on it — "this client cannot cancel" and "this job cannot be
        cancelled" are different answers.
        """
        signer = self._client.signer
        if signer is None:
            raise ValidationError(
                "cancelling a job means signing Cancel(jobId, issuedAt) with the "
                "wallet that owns it — the coordinator only relays that signature "
                "and cannot author one. This client holds no wallet, so nothing "
                "was sent: set VORQ_WALLET_KEY or pass signer=...",
                type="invalid_request_error",
            )
        ctx = await self._client.chain_context()
        issued_at = int(time.time())
        signature = signer.sign_cancel(self.id, issued_at, ctx)
        return await self._client._request(
            "POST",
            f"/v1/jobs/{self.id}/cancel",
            json={"issued_at": issued_at, "signature": signature},
        )

    async def cancel(self) -> None:
        """Cancel the job: sign ``Cancel`` and post it to ``/v1/jobs/{id}/cancel``.

        Requires the owning wallet. The node relays the signature to the chain
        and answers with the relay receipt; a job a provider has already claimed
        is refused there, as a :class:`~vorq.errors.StateConflictError`.
        """
        await self._cancel_request()
