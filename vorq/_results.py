"""Result types delivered on every path.

``handle.result()`` normalizes to :class:`TextResult` / :class:`MediaResult`
values. The API states terms and counts, never totals — ``.cost`` is computed here from the signed rates and the
job's unit counts, the same arithmetic that settles on-chain.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from decimal import Context, Decimal, Inexact, InvalidOperation, Rounded, localcontext
from pathlib import Path
from typing import TYPE_CHECKING, Union

from ._money import wire_usd
from .errors import ResultIntegrityError, VorqError

if TYPE_CHECKING:
    from ._crypto import Cipher

#: ``(rate_in, rate_out)`` in USD per 1M units of work.
Rates = tuple[Decimal | None, Decimal | None]


def _rates(vorq: dict) -> Rates:
    """The signed rates as the job view carries them: absent, or canonical USD strings."""
    return tuple(  # type: ignore[return-value]
        None if vorq.get(f) is None else wire_usd(vorq[f], f"vorq.{f}")
        for f in ("rate_in", "rate_out")
    )


#: Exact arithmetic for a cost: anything this context would round raises instead.
_EXACT = Context(prec=200, traps=[Inexact, Rounded, InvalidOperation])

#: ``JobRegistry.RATE_SCALE`` — a rate is USD per this many units of work.
RATE_SCALE = 1_000_000


def _usd(units_times_rate: Decimal) -> str:
    """``Σ units × rate / RATE_SCALE`` as a plain USD string, exact, without trailing zeros."""
    with localcontext(_EXACT):
        text = format(units_times_rate.scaleb(-6), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _text_cost(usage: dict, rates: Rates) -> str:
    rate_in, rate_out = rates
    with localcontext(_EXACT):
        total = (Decimal(usage.get("input_tokens", 0)) * (rate_in or 0)
                 + Decimal(usage.get("output_tokens", 0)) * (rate_out or 0))
    return _usd(total)


def _media_cost(units: int | Decimal, rates: Rates) -> str:
    """Output pixels (or pixel-seconds) against the signed rate.

    Divided by :data:`RATE_SCALE` like every other modality, because
    ``JobRegistry._atomicCharge`` has one code path and no media exception: a
    rate is priced per ``RATE_SCALE`` units of work whatever is being metered.
    """
    _, rate_out = rates
    with localcontext(_EXACT):
        total = Decimal(units) * (rate_out or 0)
    return _usd(total)


def _embedding_cost(usage: dict, rates: Rates) -> str:
    """Input side only, and `rate_out` is deliberately not read.

    An embeddings backend reports `prompt_tokens` and no completion count, so the job settles
    at `completionTok == 0` and the chain's charge is `rate_in * units_in`. An input-only ask
    may still publish a nonzero `rate_out` — it is inert, multiplied by zero units — and
    reading it here would display a charge nobody was ever billed.
    """
    rate_in, _ = rates
    with localcontext(_EXACT):
        total = Decimal(usage.get("prompt_tokens", 0)) * (rate_in or 0)
    return _usd(total)


# A produced frame's pixels (width × height), defaulting each dimension to 1024 to match
# the coordinator/provider so the displayed cost tracks the per-pixel rate_out.
def _frame_pixels(frame: dict) -> int:
    return int(frame.get("width", 1024) or 1024) * int(frame.get("height", 1024) or 1024)


def _flatten_response_output(output_items: list[dict]) -> str:
    """Flatten OpenAI Responses output items into their text."""
    parts: list[str] = []
    for item in output_items:
        for part in item.get("content", []) or []:
            if part.get("type") in ("output_text", "text") and "text" in part:
                parts.append(part["text"])
    return "".join(parts)


@dataclass
class TextResult:
    text: str
    output: list
    usage: dict
    raw: dict
    rates: Rates
    cost: str
    #: The flat gas fee the job was posted under, in USD, paid on top of ``cost``.
    gas_fee: Decimal
    #: The protocol fee settlement took, in USD, on top of ``cost``.
    fee: Decimal
    provider: int | str | None
    job_id: str | None
    #: The caller's own label for this line, echoed back by the provider from inside the
    #: sealed payload. ``None`` when the submission named none.
    custom_id: str | None = None


@dataclass
class MediaResult:
    frames: list[dict]
    seed: int | None
    raw: dict
    rates: Rates
    cost: str
    gas_fee: Decimal
    fee: Decimal
    provider: int | str | None
    job_id: str | None
    custom_id: str | None = None

    def bytes(self) -> list[bytes]:
        """The frames, decoded, in order.

        They travelled inside the sealed result, so this reaches no network: the
        result was fetched and opened once, by its CID, before this object existed.

        ``validate=True``, and that is the whole point of the method. Python's
        decoder **skips** characters outside the base64 alphabet and returns what
        is left, so ``b64decode("###")`` is ``b""`` — a provider could declare
        1024x768, seal garbage, and hand back a frame that read as a perfectly
        good zero-byte image, which :meth:`download` then wrote to disk. Nothing
        else on the read path would have said a word.

        A frame with no ``b64`` member is the same failure arriving as a
        ``KeyError``, which is not a :class:`~vorq.errors.VorqError` and walks
        straight through the one ``except`` a caller holds over this path. Both
        raise :class:`~vorq.errors.ResultIntegrityError`, because both mean what
        every other unreadable result means: this job has no answer to hand back.

        Whitespace is refused with everything else. ``base64.b64encode`` never
        emits any, so a frame carrying a newline did not come from a standard
        encoder, and guessing which non-alphabet characters were meant to be
        ignored is exactly the guess that produced the empty frame.
        """
        out: list[bytes] = []
        for i, frame in enumerate(self.frames):
            encoded = frame.get("b64")
            if not isinstance(encoded, str):
                raise ResultIntegrityError(
                    f"frame {i} of job {self.job_id!r} carries no base64 'b64' member, so "
                    f"there are no bytes to decode; the frame's keys are {sorted(frame)}"
                )
            try:
                out.append(base64.b64decode(encoded, validate=True))
            except binascii.Error as exc:
                raise ResultIntegrityError(
                    f"frame {i} of job {self.job_id!r} is not base64 ({exc}), so the bytes "
                    "stored under this job's result_cid are not a frame"
                ) from exc
        return out

    def download(self, dir: str | Path) -> list[Path]:
        """Write every frame into ``dir``, returning the local paths in order."""
        target = Path(dir)
        target.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for i, (frame, data) in enumerate(zip(self.frames, self.bytes())):
            path = target / f"{i}{_SUFFIXES.get(frame.get('content_type', ''), '.bin')}"
            path.write_bytes(data)
            paths.append(path)
        return paths


@dataclass
class EmbeddingResult:
    """A settled embeddings job: the OpenAI ``EmbeddingResponse``, opened.

    The provider seals that response verbatim, so ``raw`` is byte-for-byte what an OpenAI
    embeddings call would have returned and ``embeddings`` is its ``data`` array unchanged.
    """

    embeddings: list[dict]
    model: str | None
    prompt_tokens: int
    raw: dict
    rates: Rates
    cost: str
    gas_fee: Decimal
    fee: Decimal
    provider: int | str | None
    job_id: str | None
    custom_id: str | None = None

    def bytes(self) -> list[bytes]:
        """The vectors, decoded, in order.

        Assumes ``encoding_format="base64"`` — the default this SDK's jobs request, because a
        float32 vector is roughly a quarter the size that way. A response taken in ``float``
        format carries lists of numbers instead; read ``.embeddings`` directly for those.
        """
        out: list[bytes] = []
        for entry in self.embeddings:
            vector = entry.get("embedding")
            if not isinstance(vector, str):
                raise VorqError(
                    "this result's vectors are not base64: the request asked for "
                    "encoding_format='float'. Read .embeddings directly."
                )
            out.append(base64.b64decode(vector))
        return out


_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
             "video/mp4": ".mp4", "video/webm": ".webm"}


def _is_embedding(output: dict) -> bool:
    """An OpenAI embeddings response, told apart by its own `object` discriminator.

    A Responses object is ``"response"`` and a chat completion is ``"chat.completion"``, so
    ``"list"`` is unambiguous here; the per-entry check keeps some other future list shape from
    being read as vectors.
    """
    if not isinstance(output, dict) or output.get("object") != "list":
        return False
    data = output.get("data")
    return isinstance(data, list) and all(
        isinstance(entry, dict) and "embedding" in entry for entry in data
    )


def _embedding_result(
    output: dict, rates: Rates, gas_fee: Decimal, fee: Decimal, provider, job_id, custom_id
) -> EmbeddingResult:
    usage = output.get("usage") or {}
    return EmbeddingResult(
        embeddings=output.get("data") or [],
        model=output.get("model"),
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        raw=output,
        rates=rates,
        cost=_embedding_cost(usage, rates),
        gas_fee=gas_fee,
        fee=fee,
        provider=provider,
        job_id=job_id,
        custom_id=custom_id,
    )


def _is_media(output: dict) -> bool:
    return isinstance(output, dict) and ("images" in output or "video" in output)


def _media_result(
    output: dict, rates: Rates, gas_fee: Decimal, fee: Decimal,
    provider: int | str | None, job_id: str | None, custom_id: str | None,
) -> MediaResult:
    if "images" in output:
        frames = list(output.get("images", []))
        # Output pixels, summed over the delivered images — the image billing unit.
        units: int | Decimal = sum(_frame_pixels(f) for f in frames)
    else:
        video = output.get("video", {})
        frames = [video] if video else []
        # Pixel-seconds — the video billing unit.
        units = _frame_pixels(video) * int(video.get("duration_secs", 0) or 0)
    # The provider's own statement of what settled, when it makes one: frames are
    # labelled with what was delivered, the order's `units_out` caps the charge,
    # and a render larger than its cap makes the two differ.
    stated = output.get("units")
    if isinstance(stated, int) and not isinstance(stated, bool) and 0 <= stated <= units:
        units = stated
    return MediaResult(
        frames=frames,
        seed=output.get("seed"),
        raw=output,
        rates=rates,
        cost=_media_cost(units, rates),
        gas_fee=gas_fee,
        fee=fee,
        provider=provider,
        job_id=job_id,
        custom_id=custom_id,
    )


def _text_result_from_response(
    response: dict, rates: Rates, gas_fee: Decimal, fee: Decimal,
    provider: int | str | None, job_id: str | None, custom_id: str | None,
) -> TextResult:
    output_items = response.get("output", []) or []
    usage = dict(response.get("usage", {}) or {})
    return TextResult(
        text=_flatten_response_output(output_items),
        output=output_items,
        usage=usage,
        raw=response,
        rates=rates,
        cost=_text_cost(usage, rates),
        gas_fee=gas_fee,
        fee=fee,
        provider=provider,
        job_id=job_id,
        custom_id=custom_id,
    )


def _text_result_from_chat_completion(
    body: dict, rates: Rates, gas_fee: Decimal, fee: Decimal,
    provider: int | str | None, job_id: str | None, custom_id: str | None,
) -> TextResult:
    choices = body.get("choices", []) or []
    text = "".join(c.get("message", {}).get("content") or "" for c in choices)
    raw_usage = body.get("usage", {}) or {}
    usage = {
        "input_tokens": raw_usage.get("prompt_tokens", 0),
        "output_tokens": raw_usage.get("completion_tokens", 0),
        "total_tokens": raw_usage.get("total_tokens", 0),
    }
    return TextResult(
        text=text,
        output=choices,
        usage=usage,
        raw=body,
        rates=rates,
        cost=_text_cost(usage, rates),
        gas_fee=gas_fee,
        fee=fee,
        provider=provider,
        job_id=job_id,
        custom_id=custom_id,
    )


def _decrypt_output(output: dict, cipher: "Cipher | None") -> dict:
    """Open a ``vorq-sealed-v1`` result sealed to our key; pass cleartext through.

    A seal that does not open is a :class:`~vorq.errors.ResultIntegrityError`
    like every other unreadable result, and for the same reason: the caller asked
    for an answer and there is none to give. The underlying cipher's own
    exception is wrapped rather than allowed to escape — a caller holding one
    ``except VorqError`` over the read path should not also have to know which
    crypto library opened the box — and it is chained, so the cause is still
    there for anyone debugging a key mismatch.
    """
    if not (isinstance(output, dict) and output.get("enc") == "vorq-sealed-v1"):
        return output
    if cipher is None:
        raise ResultIntegrityError(
            "result is sealed but no cipher is configured to open it"
        )
    try:
        plaintext = cipher.decrypt(base64.b64decode(output["ciphertext"]))
    except Exception as exc:
        raise ResultIntegrityError(
            "the sealed result did not open with this client's result key. It was "
            "sealed to the result_key the submission's envelope carried, which a "
            f"wallet-backed client derives from its own wallet ({exc})"
        ) from exc
    return json.loads(plaintext)


def open_result_bytes(raw: bytes, cid: str, cipher: "Cipher | None") -> dict:
    """Open the bytes stored under the CID a job settled with.

    ``cid`` is the name these bytes were fetched by and is carried here for the
    errors to quote: the storage layer serves back what it stored, so the name
    identifies the result rather than challenging it.

    Bytes that are not a JSON result object raise
    :class:`~vorq.errors.ResultIntegrityError` — nothing at that name is readable
    as a result — so callers meet one shaped error the transports already carry,
    rather than a decoder's own exception.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ResultIntegrityError(
            f"result bytes under CID {cid!r} are not JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ResultIntegrityError(
            f"result bytes under CID {cid!r} decode to {type(parsed).__name__}, "
            "not a result object"
        )
    return _decrypt_output(parsed, cipher)


def _result_from_output(output: dict, job: dict) -> Union[TextResult, MediaResult, EmbeddingResult]:
    """Dispatch an opened output onto its result type, under the job's terms."""
    vorq = job.get("vorq", {}) or {}
    rates = _rates(vorq)
    gas_fee = wire_usd(vorq.get("gas_fee"), "vorq.gas_fee")
    fee = wire_usd(vorq.get("fee"), "vorq.fee")
    # The provider's correlation stamp, lifted off the sealed body before anything else reads
    # it: `raw` stays the model's own object, and the stamp is VORQ's, added outside it.
    # Only the caller's label is surfaced — the stamp's `job_id` is there for a human reading
    # a sealed body, and nothing here branches on it.
    stamp = output.get("vorq") if isinstance(output, dict) else None
    stamp = stamp if isinstance(stamp, dict) else {}
    if stamp:
        output = {k: v for k, v in output.items() if k != "vorq"}
    custom_id = stamp.get("custom_id")
    # `provider_id`, which is what the row carries — `clientJob` projects the
    # chain's `providerId` and there is no `provider` key on it. Reading the
    # wrong one is invisible: the result builds and `.provider` is just None.
    provider = vorq.get("provider_id")
    job_id = job["id"]
    if _is_embedding(output):
        return _embedding_result(output, rates, gas_fee, fee, provider, job_id, custom_id)
    if _is_media(output):
        return _media_result(output, rates, gas_fee, fee, provider, job_id, custom_id)
    if "choices" in output:
        # A job settled through the chat-completions preset carries a verbatim
        # ``chat.completion`` object; the native surface passes it through as-is,
        # so the chat-completion shape is parsed rather than the Responses one.
        return _text_result_from_chat_completion(output, rates, gas_fee, fee, provider, job_id, custom_id)
    return _text_result_from_response(output, rates, gas_fee, fee, provider, job_id, custom_id)


def result_from_raw(
    raw: bytes, job: dict, cipher: "Cipher | None" = None
) -> Union[TextResult, MediaResult, EmbeddingResult]:
    """Build the result for a settled job from its fetched result bytes.

    The only path a settled job's result is read on: the bytes fetched by the
    job's ``result_cid`` are opened with ``cipher`` (a sealed body was sealed to
    the ``result_key`` the submission's envelope carried), then dispatched onto
    their result type. There is no unnamed variant — a job that settled without
    naming its result named nothing to fetch.
    """
    output = open_result_bytes(raw, job.get("result_cid") or "", cipher)
    return _result_from_output(output, job)


@dataclass
class JobError:
    """One line of a batch that never delivered.

    ``type`` is the cause in the one canonical vocabulary every VORQ surface
    uses — ``provider_fail``, ``reclaim``, ``cancelled``, ``expired`` for a line
    that became a job, or the coordinator's own refusal code (``invalid_json``,
    ``invalid_order_signature``, ``DuplicateJob``, …) for one that never did. A
    client cancel and an order nobody claimed before its deadline are different
    facts, and this keeps them different.

    ``custom_id`` is ``None`` here and that is structural rather than missing: the
    label rides sealed inside the container, and an error row has no sealed result
    to read it back out of. Correlate on ``job_id`` — the content job id, listed
    in input order on ``BatchHandle.job_ids``.
    """

    message: str
    type: str
    job_id: str | None
    custom_id: str | None
    raw: dict = field(default_factory=dict)


def result_from_batch_line(
    line: dict, cipher: "Cipher | None" = None, raw: bytes | None = None
) -> Union[TextResult, MediaResult, EmbeddingResult, JobError]:
    """Build a result or an error from one row of a batch output/error file.

    **The named bytes are the only bytes.** A row carries ``vorq.result_cid`` and
    a ``response.body`` that is always null: the result is sealed to this client's
    own result key, so the coordinator cannot read it and does not pretend to.
    The old surface's inline "convenience copy" is gone with the thing it was a
    copy of, and with it the one way sealed bodies could be swapped between lines.

    Each row carries its own line's rates — every line settles under its own
    signed order — so cost is computed from those and never from a batch-level
    average.
    """
    vorq = line.get("vorq") or {}
    # `None` when the row carries a `vorq` block naming no job, and that is the
    # answer rather than a fallback: a line the coordinator skipped never became a
    # job, so there is no job id, and reporting the synthetic row id as one would
    # hand a caller a string that resolves to nothing on chain. Those lines
    # correlate through `BatchHandle.job_ids`, in input order. The row id is used
    # only when there is no `vorq` block at all.
    job_id = vorq.get("job_id") if "vorq" in line else line.get("id")
    error = line.get("error")
    if error:
        return JobError(
            message=error.get("message", ""),
            type=error.get("code", "unknown"),
            job_id=job_id,
            custom_id=line.get("custom_id"),
            raw=error,
        )
    result_cid = vorq.get("result_cid")
    if not result_cid:
        # A success row that names nothing named nothing to fetch. Fail closed
        # rather than inventing an empty result: the row claims a delivery and
        # there is no way to check it.
        raise ResultIntegrityError(
            f"batch row {line.get('id')!r} reports success and names no result_cid, "
            "so there are no bytes to open"
        )
    output = open_result_bytes(raw if raw is not None else b"", result_cid, cipher)
    return _result_from_output(
        output,
        {
            "id": job_id,
            "vorq": {
                "rate_in": vorq.get("rate_in"),
                "rate_out": vorq.get("rate_out"),
                "gas_fee": vorq.get("gas_fee"),
                "fee": vorq.get("fee"),
                "provider_id": vorq.get("provider"),
            },
        },
    )
