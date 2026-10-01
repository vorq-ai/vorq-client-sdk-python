"""End-to-end sealed transport for the stock ``openai`` client.

``sealing_http_client()`` returns an ``httpx.Client`` whose transport intercepts
the OpenAI Responses surface and runs the native sealed-job flow underneath:
seal the request to the serving provider, submit the order, wait (sync) or poll
(background), open the sealed result, and synthesize a standard Response object.
The coordinator never sees the request params or the generated text.

    from openai import OpenAI
    import vorq

    client = OpenAI(base_url=VORQ_URL, api_key="unused",
                    http_client=vorq.sealing_http_client())

Sync-only by design: this is the blocking-code path (the native ``vorq`` SDK is
the async surface). The wallet comes from ``VORQ_WALLET_KEY`` and derives the
result cipher with it; ``signer=`` / ``cipher=`` override either one.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

import httpx

from ._client import DEFAULT_BASE_URL, Client
from ._crypto import Cipher, Signer
from ._handles import _end_cause
from ._results import EmbeddingResult, MediaResult, TextResult
from .errors import JobFailed, ValidationError, VorqError

if TYPE_CHECKING:  # attestation verification is opt-in; importing it is not needed to use this
    from .verify import Verifier

_T = TypeVar("_T")

_STATUS = {"queued": "queued", "in_progress": "in_progress", "completed": "completed",
           "failed": "failed", "cancelled": "cancelled"}

# The coordinator routes this transport forwards unsealed, as ``(method, path)``
# with ``*`` standing for one path segment. Anything not listed here and not
# intercepted above is refused before its body is read, so a route leaks only if
# it was declared content-free deliberately — never by being overlooked. That is
# the whole point of the direction: the OpenAI surface grows, and a list of what
# must NOT be sent can never be shown complete.
#
# No entry here submits a prompt, and that is the only property this list
# asserts. The batch-create body is the one exception to "carries nothing of
# yours": its optional ``metadata`` is a plaintext field by design and travels in
# the clear. That is a property of the batch API rather than of this transport.
#
# **`POST /v1/files` is deliberately not on this list, and it is the one file
# route that is not.** A stock caller doing `client.files.create(file=...,
# purpose="batch")` uploads a plaintext JSONL — every prompt in it, in the clear —
# which is precisely the disclosure this transport exists to prevent. The reads
# are content-free and forward: the object, and the bytes of an output file that
# were sealed before they were ever written.
_FORWARD: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/models"),
    ("POST", "/v1/batches"),
    ("GET", "/v1/batches"),
    ("GET", "/v1/batches/*"),
    ("POST", "/v1/batches/*/cancel"),
    ("GET", "/v1/files/*"),
    ("GET", "/v1/files/*/content"),
    ("GET", "/v1/jobs/*"),
    ("POST", "/v1/jobs/*/cancel"),
)

# Response headers a forwarded call carries back. ``x-request-id`` is what the
# openai package attaches to its exceptions; everything else is the coordinator's
# transport detail.
#
# ``x-incomplete`` / ``x-last-line`` are gone with the thing they described. A
# batch output file is frozen and pinned once, at the end — content-addressed
# storage is immutable, so there is no partial file to report the completeness of.
_PASSTHROUGH_HEADERS = ("x-request-id",)

_SEALED_SURFACE_HINT = ("Use the Responses surface (POST /v1/responses), which seals the "
                        "payload end-to-end.")
_FILE_UPLOAD_HINT = ("A batch input file is uploaded already sealed, line by line: use "
                     "client.batches.submit(requests), which seals each line into its own "
                     "container before anything leaves the process. Uploading the file through "
                     "this transport would put every prompt in it on the wire in the clear.")


def _matches(pattern: str, path: str) -> bool:
    """Match a path against a pattern where ``*`` stands for exactly one segment.

    Segment counts must agree and a ``*`` never matches an empty segment, so
    ``/v1/jobs/`` is not ``/v1/jobs/*`` and ``/v1/responses/a/b`` is not
    ``/v1/responses/*``. The intercepts and the forward list share this so the
    file has one matching rule rather than one per caller.
    """
    parts, segments = pattern.split("/"), path.split("/")
    return len(parts) == len(segments) and all(
        (p == "*" and s != "") or p == s for p, s in zip(parts, segments))


def _forwardable(method: str, path: str) -> bool:
    """Whether this route is declared free of prompt content, so it may go unsealed."""
    return any(m == method and _matches(pattern, path) for m, pattern in _FORWARD)


def _deny_hint(path: str) -> str:
    """The surface to point a refused caller at instead.

    It used to say batches were gated, which stopped being true the moment the
    routes came back. What it says now is the one thing still refused here and
    why: the **upload**, because a stock client's batch file is plaintext.
    """
    return _FILE_UPLOAD_HINT if path.startswith("/v1/files") else _SEALED_SURFACE_HINT


def _response_object(job: dict, *, model: str | None = None, background: bool,
                     result: TextResult | MediaResult | EmbeddingResult | None = None) -> dict:
    """Render an OpenAI Response from whatever the coordinator just answered.

    **Two wire shapes name a job and they spell it differently.** A row from
    `GET /v1/jobs/{id}` is keyed `id`; a relay receipt from `POST /v1/jobs` (and
    from the cancel) is keyed `job_id` and carries no `status` at all. Both reach
    here — a background create renders the receipt, a retrieve renders the row —
    so reading `job["id"]` alone raises `KeyError` on every fresh submission,
    which the `openai` package then reports as `APIConnectionError`: the network
    blamed for a mapping that never met the real node.

    A receipt has no status because it does not need one: the node waits for the
    transaction receipt before answering, so a post that returned is a job on the
    book — `queued` — and a cancel that returned is a job the chain has ended.
    """
    status = _STATUS.get(job.get("status", "queued"), "queued")
    out: dict[str, Any] = {
        "id": job.get("id") or job["job_id"], "object": "response", "status": status,
        "model": model or job.get("model"), "background": background,
        "created_at": job.get("created_at") or int(time.time()),
        "output": [], "incomplete_details": None,
        "metadata": job.get("metadata") or {}, "vorq": job.get("vorq") or {},
    }
    if isinstance(result, MediaResult):
        out["status"] = "completed"
        # An OpenAI client consumes bytes. The frames are already base64 inside the
        # result, so they pass straight through — decoding to re-encode would be
        # the same string and twice the work.
        #
        # Video frames render under the same item type: the Responses schema has
        # no video item, and the type is the consumer's only signal for what
        # ``result`` decodes to, so the frame's ``content_type`` is what a caller
        # reads off the native MediaResult to tell the two apart.
        out["output"] = [
            {"type": "image_generation_call", "id": f"ig_{out['id']}_{i}",
             "status": "completed", "result": frame.get("b64")}
            for i, frame in enumerate(result.frames)
        ]
    elif isinstance(result, TextResult):
        # `TextResult` and not "anything that is not media": an `EmbeddingResult`
        # has no `.text` and no Responses item type to render as, and this
        # transport's surface is the Responses API. One reaching here would mean a
        # job settled through the embeddings preset was retrieved as a response,
        # and the honest render of that is the job row without an output — not a
        # fabricated message.
        out["status"] = "completed"
        out["output"] = [{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": result.text}]}]
        out["usage"] = {
            "input_tokens": result.usage.get("input_tokens", 0),
            "output_tokens": result.usage.get("output_tokens", 0),
            "total_tokens": result.usage.get("total_tokens", 0),
        }
    if status in ("failed", "cancelled"):
        # The row carries no `error` object — the end cause is `vorq.ended_because`
        # (`Types.sol`: 2 cancelled, 3 provider_fail, 4 reclaim, 5 expired). Read
        # through the same table the native surface uses, so `responses.retrieve`
        # reports the cause `handle.result()` would have raised instead of
        # reporting nothing. An explicit `error` on the body still wins.
        error = job.get("error")
        cause = error.get("code") if isinstance(error, dict) else _end_cause(job)
        if error or cause:
            out["error"] = error if isinstance(error, dict) else {
                "code": cause, "message": f"job {out['id']} {status} ({cause})"}
    return out


class SealingTransport(httpx.BaseTransport):
    """Intercepts the Responses surface and runs the sealed job flow underneath.

    Two different base URLs, on purpose. ``OpenAI(base_url=...)`` needs the
    ``/v1`` suffix — that is what the ``openai`` package uses to build the
    request path (``/v1/responses``), which is what :meth:`handle_request`
    pattern-matches on. This transport's own ``base_url`` takes the bare
    coordinator origin instead (no ``/v1``), since it talks to ``/v1/jobs`` and
    friends directly rather than through the ``openai`` package's path building.

    Both the intercepts and the forward list match ``/v1/...`` exactly, so an
    ``OpenAI`` base URL carrying a mount prefix (``https://host/api/v1``)
    presents paths as ``/api/v1/...`` and matches neither — every call is
    refused, including Responses. The mount prefix is a misconfiguration and it
    reads as one; it does not silently unseal anything. Pair a root-mounted
    coordinator with the ``/v1`` suffix on the ``openai`` client.
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, signer: Signer | None = None,
                 cipher: Cipher | None = None, verifier: "Verifier | None" = None,
                 timeout: float = 30.0,
                 inner_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._signer = signer
        self._cipher = cipher
        # Needed for an open order (a bid named, no provider): it seals to the
        # coordinator's escrow key, and the native client will not post one it
        # cannot verify (Q17).
        self._verifier = verifier
        self._timeout = timeout
        self._inner_transport = inner_transport  # test seam for the native client

    # -- plumbing ----------------------------------------------------------

    def _run(self, fn: Callable[[Client], Awaitable[_T]]) -> _T:
        """Run one native-client operation on a private event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "sealing_http_client is sync-only; inside an event loop use vorq.Client directly"
            )

        async def go() -> _T:
            client = Client(base_url=self._base_url, timeout=self._timeout,
                            signer=self._signer, cipher=self._cipher,
                            verifier=self._verifier,
                            transport=self._inner_transport)
            try:
                return await fn(client)
            finally:
                await client.aclose()

        return asyncio.run(go())

    def _json(self, status: int, body: dict,
              headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, json=body,
                              headers={"content-type": "application/json", **(headers or {})})

    def _error(self, exc: VorqError, *, retryable: bool = True) -> httpx.Response:
        # The status the error arrived on, so the openai package raises the class
        # a caller expects — and, for 429/5xx, still applies its own retry policy,
        # which it does not do for a 400. An error the SDK raised locally has no
        # status; those are all request-shape refusals, hence 400.
        #
        # ``retryable=False`` opts a route out of that policy with the header the
        # openai package checks ahead of the status. Submissions need it: the
        # native client passes retry=False on POST /v1/jobs precisely so a job is
        # never duplicated, and a re-seal produces a fresh job id, so the
        # coordinator's duplicate-id guard cannot collapse the copies either. A
        # retried 429 there would mean paying twice for one call.
        headers = {"x-request-id": exc.request_id} if exc.request_id else {}
        if not retryable:
            headers["x-should-retry"] = "false"
        return self._json(exc.status_code or 400, {"error": {
            "message": str(exc), "type": exc.type or "invalid_request_error"}}, headers)

    # -- interception ------------------------------------------------------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/responses":
            return self._create(json.loads(request.read() or b"{}"))
        if request.method == "POST" and _matches("/v1/responses/*/cancel", path):
            # ``client.responses.cancel(id)``. A response id IS the job id here, so
            # this is the native cancel under a different name; the coordinator has
            # no /v1/responses/{id}/cancel route to forward to.
            return self._cancel(path.split("/")[3])
        if request.method == "GET" and _matches("/v1/responses/*", path):
            return self._retrieve(path.split("/")[3])
        if _forwardable(request.method, path):
            return self._forward(request)
        # Refused here, before ``_forward`` reads and transmits the body, so the
        # prompt stays local. The message says so: a caller that sees a bare
        # transport error cannot tell whether its content was disclosed.
        return self._error(ValidationError(
            f"{path} is not sealed by this transport, so the request was not sent. "
            + _deny_hint(path)))

    def _create(self, body: dict) -> httpx.Response:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            # The model selects the provider and the rates it clears at, so an
            # order without one is not submittable. Refused here rather than
            # sealed and sent: the coordinator would reject it, and the caller
            # would have paid a round trip to learn what is knowable locally.
            return self._error(ValidationError(
                "'model' is required and must be a model name; see client.models.list()."))
        if body.get("stream"):
            # The openai package treats a non-SSE 200 as an empty event stream, so
            # accepting this would hand the caller a silent empty iterator instead
            # of their answer. A sealed result is one object, opened at settle.
            return self._error(ValidationError(
                "streaming is not available on the sealed surface: a sealed result is a "
                "single object, delivered when the job settles. Omit 'stream', or use "
                "background=True and poll retrieve."))
        if body.get("metadata"):
            # Forbidden as a job param, for the same reason as 'user': a stable
            # caller-chosen identifier would link a wallet's jobs to each other
            # across every provider that serves them. The rule is "rejected, not
            # quietly dropped" — dropping it and echoing {} back reads as stored.
            # Scoped to this surface deliberately: batch-level metadata is a
            # different, plaintext-by-design field, and is not affected.
            return self._error(ValidationError(
                "'metadata' is not carried by the sealed Responses surface: a stable "
                "caller-chosen identifier would link your jobs to each other across "
                "providers, and nothing on the network reads it. Keep it locally, keyed "
                "by the response id."))
        background = body.get("background") is True
        # Everything an order needs that OpenAI's body has no field for: the SLA
        # window, the bid, and an optional named provider. `rate_in`/`rate_out`
        # are USD per 1M units as decimal strings; with neither, the order takes
        # the market (the first ask the node ranks), exactly as `submit` does.
        vorq_block = body.get("vorq") or {}
        sla = vorq_block.get("sla") or "1h"
        payload = {k: v for k, v in body.items()
                   if k not in ("model", "background", "vorq", "metadata")}
        if isinstance(payload.get("input"), list):
            payload["messages"] = payload.pop("input")

        async def go(client: Client) -> dict:
            handle = await client.submit(model, payload, sla=sla,
                                         rate_in=vorq_block.get("rate_in"),
                                         rate_out=vorq_block.get("rate_out"),
                                         provider=vorq_block.get("provider"))
            if background:
                job = handle._job or {"id": handle.id, "status": "queued"}
                return _response_object(job, model=model, background=True)
            try:
                result = await handle.result()
            except JobFailed as exc:
                return _response_object(
                    {"id": handle.id, "status": "failed",
                     "error": {"code": exc.error_type, "message": str(exc)}},
                    model=model, background=False)
            return _response_object(handle._job or {"id": handle.id, "status": "completed"},
                                    model=model, background=False, result=result)

        try:
            return self._json(200, self._run(go))
        except VorqError as exc:
            return self._error(exc, retryable=False)

    def _retrieve(self, response_id: str) -> httpx.Response:
        async def go(client: Client) -> dict:
            handle = client.job(response_id)
            job = await handle._fetch()
            model = job.get("model")
            if job.get("status") == "completed":
                # Same read as the native surface — the named result is fetched
                # and opened here too.
                result = await handle._settled_result(job)
                if isinstance(result, (TextResult, MediaResult)):
                    return _response_object(job, model=model, background=True, result=result)
            return _response_object(job, model=model, background=True)

        try:
            return self._json(200, self._run(go))
        except VorqError as exc:
            return self._error(exc)

    def _cancel(self, response_id: str) -> httpx.Response:
        async def go(client: Client) -> dict:
            # Routed through the handle rather than posted here, because the
            # cancel is a signed chain op: the body carries `{issued_at,
            # signature}` over `Cancel(jobId, issuedAt)` and there is exactly one
            # place in this SDK that authors it.
            #
            # **The answer is a relay receipt, not a job.** The node's cancel
            # route ends in `relay(chain, ..., reply, 200, { job_id: jobId })`,
            # so the body is `{job_id, tx_hash}` — no `id`, no `status`, no
            # terms. This used to demand an `id` and raise "returned no job
            # object" on a cancel that had in fact succeeded.
            #
            # `cancelled` is asserted rather than re-read, and it is not
            # optimism: `relay` waits for the transaction receipt before it
            # answers, so a `200` here means the chain has ended this job. A
            # follow-up `GET` would cost a round trip and open a window for a
            # state the cancel did not produce.
            resp = await client.job(response_id)._cancel_request()
            try:
                receipt = resp.json()
            except json.JSONDecodeError:
                receipt = None
            job_id = receipt.get("job_id") if isinstance(receipt, dict) else None
            if not isinstance(job_id, str):
                # A 2xx whose body names no job. Named as such rather than left
                # to escape as a KeyError or JSONDecodeError, which the openai
                # package wraps as APIConnectionError — blaming the network for a
                # coordinator that answered.
                raise VorqError(
                    f"cancel of {response_id} was accepted but the answer named no "
                    f"job_id: {receipt!r}",
                    type="invalid_response")
            return _response_object({"id": job_id, "status": "cancelled"},
                                    background=True)

        try:
            return self._json(200, self._run(go))
        except VorqError as exc:
            return self._error(exc)

    def _forward(self, request: httpx.Request) -> httpx.Response:
        raw = request.read()
        body = json.loads(raw) if raw else None

        async def go(client: Client) -> tuple[int, bytes, dict[str, str]]:
            # The caller's Authorization header is intentionally dropped: session
            # auth rides the wallet underneath, minted and rotated by the native
            # client, so the request re-authenticates rather than replays a token.
            resp = await client._request(request.method, request.url.path,
                                         json=body, params=dict(request.url.params) or None)
            headers = {"content-type": resp.headers.get("content-type", "application/json")}
            for name in _PASSTHROUGH_HEADERS:
                if name in resp.headers:
                    headers[name] = resp.headers[name]
            return resp.status_code, resp.content, headers

        try:
            status, content, headers = self._run(go)
        except VorqError as exc:
            # POST /v1/batches creates a batch, so replaying it bills twice; every
            # other forwarded route is a read or an idempotent cancel.
            creates = request.method == "POST" and request.url.path == "/v1/batches"
            return self._error(exc, retryable=not creates)
        return httpx.Response(status, content=content, headers=headers)


def sealing_http_client(*, base_url: str = DEFAULT_BASE_URL, signer: Signer | None = None,
                        cipher: Cipher | None = None, verifier: "Verifier | None" = None,
                        timeout: float = 30.0,
                        inner_transport: httpx.AsyncBaseTransport | None = None) -> httpx.Client:
    """An ``httpx.Client`` for ``OpenAI(http_client=...)`` with E2E-sealed payloads.

    Pass ``verifier=vorq.Verifier(base_url)`` to submit **open** orders (a bid
    named in the ``vorq`` block with no provider): without one the native client
    underneath fails closed on the escrow key. A call that names no bid takes the
    market, pinned to a provider, and needs no verifier.
    """
    transport = SealingTransport(base_url=base_url, signer=signer, cipher=cipher,
                                 verifier=verifier, timeout=timeout,
                                 inner_transport=inner_transport)
    return httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

