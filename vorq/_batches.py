"""The ``batches`` namespace and ``BatchHandle``.

A batch fans many requests out at once, and **every line is sealed before
anything is uploaded**. Each line is a complete submission — the same order, the
same container v1, the same payment authorization that ``POST /v1/jobs`` takes — so the
file the coordinator receives carries routing terms and ciphertext and nothing a
reader could act on. The coordinator splits it, files each container, and lands
the lines on chain in as few transactions as the block will take.

That is the whole answer to the design gap batches were gated on: the JSONL is no
longer a payload the node keeps, it is a file of sealed envelopes it pins like
every other container. Results come back sealed to this client's own key, named on
each output row by ``result_cid``, and are read from the storage gateway rather
than from the file — the row names the bytes, it never carries them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from itertools import islice
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

from ._money import format_usd, usd_arg, usd_atomic, wire_atomic
from ._params import check_input
from ._results import (
    EmbeddingResult,
    JobError,
    MediaResult,
    TextResult,
    result_from_batch_line,
)
from . import _sla
from ._sla import batch_poll_interval, normalize_sla, sla_seconds
from .errors import BatchFailed, ValidationError, VorqError, WaitTimeout

if TYPE_CHECKING:
    from ._client import Client, SealedLine

_TERMINAL = {"completed", "failed", "expired", "cancelled"}

#: The two endpoints a batch line may name. Chat completions is deliberately not
#: one: this SDK's sealed surface is the Responses API, and a batch is the same
#: submission fifty thousand times over.
_ENDPOINTS = ("/v1/responses", "/v1/embeddings")
_DEFAULT_ENDPOINT = "/v1/responses"

Result = Union[TextResult, MediaResult, EmbeddingResult]
ResultCallback = Callable[[Result], Any]
ErrorCallback = Callable[[JobError], Any]


def _load_requests(requests: list[dict] | str) -> list[dict]:
    """Accept a list of OpenAI batch request lines or a path to a JSONL file."""
    if isinstance(requests, str):
        with open(requests, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    return list(requests)


def _endpoint_of(line: dict, n: int) -> str:
    """The endpoint one line names, defaulting to the Responses surface.

    An embeddings line and a responses line settle differently — one is metered on
    input alone — so a batch is for one endpoint and the create call says which.
    A file mixing the two is refused here rather than at the coordinator, because
    the caller can see which line disagrees and the coordinator cannot.
    """
    url = line.get("url", _DEFAULT_ENDPOINT)
    if url not in _ENDPOINTS:
        raise ValidationError(
            f"line {n}: url must be one of {', '.join(_ENDPOINTS)}, got {url!r}",
            type="invalid_request_error",
        )
    return url


#: What lines are planned together by: the model and the two atomic ceilings.
_Key = tuple[str, Union[int, None], Union[int, None]]


class Batches:
    """``client.batches`` — submit a sealed batch, or re-attach to one."""

    def __init__(self, client: "Client") -> None:
        self._client = client

    async def submit(
        self,
        requests: list[dict] | str,
        completion_window: str = "batch",
        *,
        providers: list[int] | None = None,
        metadata: dict[str, str] | None = None,
        validate_params: bool = True,
    ) -> "BatchHandle":
        """Seal, sign, pay and upload every line, then create the batch.

        ``requests`` is a list of OpenAI batch request lines (``{"custom_id",
        "method", "url", "body": {"model", ...}}``) or a path to such a JSONL
        file. Every line is sealed to its recipient inside this process; nothing
        leaves it in the clear.

        A line's ``max_rate_in`` / ``max_rate_out`` are the most it pays, in USD
        per 1M units of work, a ``str`` or ``Decimal`` (``"0.05"``); an ``int`` or
        ``float`` is refused. Each is optional and a side left out has no ceiling.
        Before anything is sealed, the coordinator plans who takes each line
        within its ceilings and at which ask, never past a provider's on-chain
        capacity; a planned line signs that provider's ask and is pinned to it.
        A line the plan cannot place **rests** at its ceilings, a side with no
        ceiling at the market rate (the cheapest live ask's). A line with no
        ceiling at all has nothing to rest at, so a batch whose such lines the
        network cannot take in full raises
        :class:`~vorq.errors.ValidationError` with nothing signed.

        ``providers`` is how the resting lines spread. Given a list, they are designated
        round-robin across it — one batch running across several operators, each
        able to open only its own lines. Given nothing, each is an **open
        order**: sealed to the coordinator's verified escrow key and claimable by
        any provider that clears the terms, which spreads further than a fixed
        list can. That path requires a client built with ``verifier=``, exactly
        as a single open ``submit`` does — an escrow key that cannot be checked
        is not one this SDK will seal to.

        ``custom_id`` is optional (1–64 characters, unique within the batch) and
        **travels sealed inside its line's container**, coming back inside the
        sealed result. It is never on the wire in the clear and the coordinator
        never holds it. Lines correlate by ``job_id`` — the content job id, listed
        in input order on :attr:`BatchHandle.job_ids` — until their results are
        opened.

        One network round trip prices the whole batch: the fees on top of the cap
        are read once, from the ``402`` quote for the first line's own order, and
        every line's cap is computed locally from the rates and unit counts it signs.
        """
        window = normalize_sla(completion_window)
        client = self._client
        if client.signer is None or client.cipher is None:
            raise ValidationError(
                "batch submissions must be sealed: configure VORQ_WALLET_KEY "
                "(or pass signer=/cipher=)",
                type="invalid_request_error",
            )
        lines = _load_requests(requests)
        if not lines:
            raise ValidationError("the batch contains no requests", type="invalid_request_error")

        # -- everything checkable locally, before a single byte is sealed ----
        #
        # Sealing is the expensive half and it is per line; a file with a typo on
        # line 40 000 should cost nothing but the read.
        seen: set[str] = set()
        by_model: dict[str, list[int]] = {}
        endpoints: set[str] = set()
        for i, line in enumerate(lines):
            n = i + 1
            custom_id = line.get("custom_id")
            if custom_id is not None:
                if not isinstance(custom_id, str) or not custom_id or len(custom_id) > 64:
                    raise ValidationError(
                        f"line {n}: custom_id must be a 1-64 character string",
                        type="invalid_request_error",
                    )
                if custom_id in seen:
                    raise ValidationError(
                        f'line {n}: duplicate custom_id "{custom_id}"',
                        type="invalid_request_error",
                    )
                seen.add(custom_id)
            body = line.get("body")
            if not isinstance(body, dict) or not body.get("model"):
                raise ValidationError(
                    f"line {n}: body.model is required", type="invalid_request_error"
                )
            for key in ("max_rate_in", "max_rate_out"):
                usd_arg(body.get(key), f"line {n}: {key}")
            endpoints.add(_endpoint_of(line, n))
            by_model.setdefault(body["model"], []).append(i)

        if len(endpoints) > 1:
            raise ValidationError(
                "a batch is for one endpoint and this file names "
                f"{', '.join(sorted(endpoints))}: an embeddings line and a responses "
                "line are metered differently and settle differently",
                type="invalid_request_error",
            )
        endpoint = endpoints.pop()

        if validate_params:
            for model, indexes in by_model.items():
                try:
                    schema = await client.models.params_schema(model)
                except Exception:
                    schema = None  # a discovery hiccup never blocks a submit
                for i in indexes:
                    check_input(schema, _input_of(lines[i]))

        # -- the plan: who takes each line, at which rates --------------------
        ctx = await client.chain_context()
        picks = await self._plan(lines, window, ctx.decimals)

        # -- seal every line ------------------------------------------------
        sealed: list["SealedLine"] = []
        for i, line in enumerate(lines):
            body = dict(line["body"])
            # Split before the call rather than inside it: the routing keys and the
            # model-owned input go to different places, and a `pop` in an argument
            # list mutates the dict the neighbouring argument already points at.
            model = body.pop("model")
            body.pop("max_rate_in", None)
            body.pop("max_rate_out", None)
            units_out = body.pop("units_out", None)
            provider, rate_in, rate_out = picks[i]
            if provider is None:  # not planned: it rests
                provider = _provider_for(providers, i)
            sealed.append(
                await client._seal_line(
                    model=model,
                    payload_input=body,
                    window=window,
                    url=endpoint,
                    rate_in=rate_in,
                    rate_out=rate_out,
                    provider=provider,
                    units_out=units_out,
                    custom_id=line.get("custom_id"),
                    ctx=ctx,
                )
            )

        # -- one quote for the whole batch -----------------------------------
        gas_fee, fee_bps = await self._fees(sealed[0], ctx.decimals)
        content = (
            "\n".join(json.dumps(client._pay_line(line, gas_fee, ctx, fee_bps)) for line in sealed)
            + "\n"
        ).encode("utf-8")

        file_id = (await client._upload_file("batch.jsonl", "batch", content))["id"]
        create: dict[str, Any] = {
            "input_file_id": file_id,
            "endpoint": endpoint,
            "completion_window": window,
        }
        if metadata:
            create["metadata"] = metadata
        resp = await client._request("POST", "/v1/batches", json=create, retry=False)
        handle = BatchHandle(client, resp.json())
        handle.job_ids = [line.job_id for line in sealed]
        return handle

    async def _plan(
        self, lines: list[dict], window: str, decimals: int
    ) -> list[tuple[int | None, int, int]]:
        """Provider and atomic rates for every line, from the coordinator's plan.

        One ``POST /v1/batches`` with no file: per model and pair of ceilings, the
        line count and the summed units. The node answers which providers within
        the ceilings take how many lines, never past a provider's on-chain
        capacity, and a placed line signs its provider's ask. A line left over
        rests, with no provider (``None``), at its ceilings; a side with no
        ceiling rests at the market rate, read by one more plan that names none.
        A line with no ceiling at all cannot rest, so a batch that leaves one
        over is refused before anything is signed.
        """
        from ._client import _declare_units

        client = self._client
        groups: dict[_Key, list[int]] = {}
        units: dict[_Key, list[int]] = {}
        for i, line in enumerate(lines):
            body = line["body"]
            key = (
                body["model"],
                usd_atomic(body.get("max_rate_in"), f"line {i + 1}: max_rate_in", decimals),
                usd_atomic(body.get("max_rate_out"), f"line {i + 1}: max_rate_out", decimals),
            )
            groups.setdefault(key, []).append(i)
            u_in, u_out = _declare_units(_input_of(line), body.get("units_out"))
            total = units.setdefault(key, [0, 0])
            total[0] += u_in
            total[1] += u_out

        async def allocations(
            entries: list[tuple[_Key, int]], ceilings: bool = True
        ) -> list[list[tuple[int, int, int, int]]]:
            """Per entry ``(key, lines)``: the ``(provider, rate_in, rate_out, lines)`` shares the node plans."""
            models = []
            for key, count in entries:
                model, max_in, max_out = key if ceilings else (key[0], None, None)
                entry: dict[str, Any] = {
                    "model_id": await client._model_id(model), "lines": count,
                    "units_in": units[key][0], "units_out": units[key][1],
                }
                if max_in is not None:
                    entry["max_rate_in"] = format_usd(max_in, decimals)
                if max_out is not None:
                    entry["max_rate_out"] = format_usd(max_out, decimals)
                models.append(entry)
            resp = await client._request(
                "POST", "/v1/batches", json={"completion_window": window, "models": models},
                retry=False, allow_statuses=frozenset({402}),
            )
            plan = (resp.json() or {}).get("plan") if resp.status_code == 402 else None
            if not isinstance(plan, list) or len(plan) != len(entries):
                raise VorqError(
                    f"POST /v1/batches answered {resp.status_code} to a plan without one plan "
                    "entry per entry asked", type="api_error", status_code=resp.status_code,
                )
            shares: list[list[tuple[int, int, int, int]]] = []
            for entry in plan:
                allocation = entry.get("allocation") if isinstance(entry, dict) else None
                if not isinstance(allocation, list):
                    raise VorqError("a plan entry carries no allocation list",
                                    type="api_error", status_code=402)
                try:
                    shares.append([
                        (
                            int(share["provider_id"]),
                            wire_atomic(share["rate_in"], "allocation[].rate_in", decimals),
                            wire_atomic(share["rate_out"], "allocation[].rate_out", decimals),
                            int(share["lines"]),
                        )
                        for share in allocation
                    ])
                except (KeyError, TypeError, ValueError, VorqError) as exc:
                    raise VorqError(f"a plan allocation is unreadable: {exc}",
                                    type="api_error", status_code=402) from exc
            return shares

        keys = list(groups)
        picks: dict[int, tuple[int | None, int, int]] = {}
        left: dict[_Key, list[int]] = {}
        for key, shares in zip(keys, await allocations([(k, len(groups[k])) for k in keys])):
            _, max_in, max_out = key
            queue = iter(groups[key])
            for pid, rate_in, rate_out, count in shares:
                if (max_in is not None and rate_in > max_in) or (
                    max_out is not None and rate_out > max_out
                ):
                    raise VorqError(
                        f"the node planned provider {pid} at an ask above the ceilings the "
                        "plan set", type="api_error", status_code=402,
                    )
                for i in islice(queue, count):
                    picks[i] = (pid, rate_in, rate_out)
            rest = list(queue)
            if rest:
                left[key] = rest

        def refuse(key: _Key) -> ValidationError:
            model, placed = key[0], len(groups[key]) - len(left[key])
            return ValidationError(
                f"the network can take {placed} of the {len(groups[key])} {model} lines "
                f"without both ceilings in the {window} window right now, so nothing was "
                "signed. Split the file, try the other window, or name max_rate_in and "
                "max_rate_out on the lines",
                type="invalid_request_error",
            )

        # -- the lines that rest: at their ceilings, the market rate where there is none
        for key in left:
            if key[1] is None and key[2] is None:
                raise refuse(key)
        unnamed = [key for key in left if key[1] is None or key[2] is None]
        market: dict[_Key, tuple[int, int]] = {}
        if unnamed:
            asks = await allocations([(key, len(left[key])) for key in unnamed], ceilings=False)
            for key, shares in zip(unnamed, asks):
                if not shares:
                    raise refuse(key)
                market[key] = shares[0][1], shares[0][2]
        for key, indexes in left.items():
            _, max_in, max_out = key
            ask_in, ask_out = market.get(key, (0, 0))
            for i in indexes:
                picks[i] = (
                    None,
                    ask_in if max_in is None else max_in,
                    ask_out if max_out is None else max_out,
                )
        return [picks[i] for i in range(len(lines))]

    async def _fees(self, line: "SealedLine", decimals: int) -> tuple[int, int]:
        """What every line's payment has to cover beyond its cap, read once: the atomic gas fee and ``fee_bps``.

        A terms-only ``POST /v1/jobs`` is stateless and posts nothing — it is the
        challenge half of the single-job exchange — so asking it costs one request
        and commits nothing. Using the first line's own signed order rather than an
        invented one keeps the question honest: the answers are the fees that line
        will actually be charged, and every other line in the batch is charged the
        same fees in the same block.
        """
        resp = await self._client._request(
            "POST",
            "/v1/jobs",
            json=line.vorq,
            retry=False,
            allow_statuses=frozenset({402}),
        )
        if resp.status_code != 402:
            raise VorqError(
                f"POST /v1/jobs answered {resp.status_code} to a terms-only body; only a 402 "
                "quote is a valid answer to one, and a batch cannot price its lines without it",
                type="api_error",
                status_code=resp.status_code,
            )
        quote = (resp.json() or {}).get("quote")
        if not isinstance(quote, dict) or "gas_fee" not in quote:
            raise VorqError(
                "the quote carries no gas_fee, so this batch cannot compute what each "
                "line's payment has to cover",
                type="api_error",
                status_code=402,
            )
        fee_bps = quote.get("fee_bps")
        if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or not 0 <= fee_bps <= 1000:
            raise VorqError(
                "the quote carries no fee_bps, so this batch cannot compute what each "
                "line's payment has to cover",
                type="api_error",
                status_code=402,
            )
        return wire_atomic(quote["gas_fee"], "quote.gas_fee", decimals, 402), fee_bps

    def get(self, batch_id: str) -> "BatchHandle":
        """Re-attach to a batch from a persisted id — no network call."""
        return BatchHandle(self._client, batch_id)


def _input_of(line: dict) -> dict:
    """The model-owned input of one line: its body without the routing keys."""
    return {
        k: v
        for k, v in line["body"].items()
        if k not in ("model", "max_rate_in", "max_rate_out", "units_out")
    }


def _provider_for(providers: list[int] | None, index: int) -> int | None:
    """Round-robin, or ``None`` for an open order sealed to the escrow key."""
    if not providers:
        return None
    return providers[index % len(providers)]


async def _dispatch(cb: Callable | None, arg: Any, tasks: list[asyncio.Task]) -> None:
    """Invoke a callback; a coroutine callback is scheduled concurrently."""
    if cb is None:
        return
    outcome = cb(arg)
    if inspect.isawaitable(outcome):
        tasks.append(asyncio.ensure_future(outcome))


class BatchHandle:
    """A live batch: its status, its results, and its cancel."""

    def __init__(self, client: "Client", id_or_obj: str | dict) -> None:
        self._client = client
        #: Per-line content job ids in input order, set by :meth:`Batches.submit`.
        #: The correlation key for every line until its sealed result is opened.
        self.job_ids: list[str] | None = None
        self.output_file_id: str | None = None
        self.error_file_id: str | None = None
        self.request_counts: dict | None = None
        self._completion_window: str | None = None
        if isinstance(id_or_obj, dict):
            self.id = id_or_obj["id"]
            self._apply(id_or_obj)
        else:
            self.id = id_or_obj

    def _apply(self, batch: dict) -> None:
        self.output_file_id = batch.get("output_file_id")
        self.error_file_id = batch.get("error_file_id")
        self.request_counts = batch.get("request_counts")
        self._completion_window = batch.get("completion_window")

    async def _fetch(self) -> dict:
        resp = await self._client._request("GET", f"/v1/batches/{self.id}")
        batch = resp.json()
        self._apply(batch)
        return batch

    async def status(self) -> str:
        """One ``GET``; returns the batch status string."""
        return (await self._fetch())["status"]

    async def _read_file(
        self,
        file_id: str,
        on_result: ResultCallback | None,
        on_error: ErrorCallback | None,
        tasks: list[asyncio.Task],
    ) -> None:
        """Read one frozen file, whole.

        **No ``?offset=`` and no incremental drain.** The output file is built,
        frozen and pinned once, at the end — content-addressed storage is
        immutable, so an append would mint a different object under a different
        name, and a partially-served file and its finished successor are two
        different files rather than two views of one. A caller wanting progress
        before the end polls :meth:`status`; the file exists whole or not at all.
        """
        resp = await self._client._request("GET", f"/v1/files/{file_id}/content")
        for raw in resp.text.splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            # A row names its result and never carries it: the bytes are sealed to
            # this client's key, fetched by the name the row gives, and checked
            # against that name. There is no inline copy to fall back to, which is
            # what stops sealed bodies being swapped between lines.
            result_cid = (row.get("vorq") or {}).get("result_cid")
            body = await self._client.fetch_blob(result_cid) if result_cid else None
            parsed = result_from_batch_line(row, cipher=self._client.cipher, raw=body)
            if isinstance(parsed, JobError):
                await _dispatch(on_error, parsed, tasks)
            else:
                await _dispatch(on_result, parsed, tasks)

    async def _run(
        self,
        on_result: ResultCallback | None,
        on_error: ErrorCallback | None,
        timeout: float | None,
    ) -> None:
        batch = await self._fetch()
        window = self._completion_window or "24h"
        if timeout is None:
            timeout = sla_seconds(window)
        start = _sla.now()
        deadline = start + timeout
        tasks: list[asyncio.Task] = []

        # One read of the batch per tick, whatever its line count: the lines are
        # never polled one by one.
        while batch["status"] not in _TERMINAL:
            now = _sla.now()
            remaining = deadline - now
            if remaining <= 0:
                raise WaitTimeout(
                    f"Batch {self.id} did not settle within {timeout}s. Nothing was "
                    f"cancelled: the lines keep running and {self.id} can be re-attached "
                    "with client.batches.get(...).",
                    job_id=self.id,
                )
            await asyncio.sleep(min(batch_poll_interval(now - start), remaining))
            batch = await self._fetch()

        if batch["status"] == "failed":
            # The input file was refused, so there are no lines and no files to
            # read. Per-line failures are error-file rows and never this.
            raise BatchFailed(f"Batch {self.id} failed.", batch_id=self.id)

        # Terminal: both files are frozen, so this is one read each and never a loop.
        if self.output_file_id:
            await self._read_file(self.output_file_id, on_result, on_error, tasks)
        if self.error_file_id:
            await self._read_file(self.error_file_id, on_result, on_error, tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def consume(
        self,
        on_result: ResultCallback,
        on_error: ErrorCallback | None = None,
        timeout: float | None = None,
    ) -> None:
        """Suspend until terminal, then fire a callback per line.

        Delivery is in file order — every settled line, then every failed one —
        not input order. Correlate on ``.custom_id`` once a result is opened, or on
        ``.job_id`` (listed in input order on :attr:`job_ids`) before that and for
        every error row, which has no sealed result to read a label out of.

        Callbacks may be plain or coroutine functions; coroutine callbacks are
        dispatched concurrently.
        """
        await self._run(on_result, on_error, timeout)

    async def results(
        self, timeout: float | None = None
    ) -> list[Union[Result, JobError]]:
        """Suspend until terminal, then return the full merged result list."""
        collected: list[Any] = []
        await self._run(collected.append, collected.append, timeout)
        return collected

    async def cancel(self) -> None:
        """Cancel the batch.

        Lines still open are cancelled; a line a provider has already **claimed**
        runs to its own end, exactly as a standalone job does — nobody can take
        work back out of a provider's hands mid-flight.
        """
        await self._client._request("POST", f"/v1/batches/{self.id}/cancel")


__all__ = ["Batches", "BatchHandle"]
