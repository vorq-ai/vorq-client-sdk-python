import base64
import json
from decimal import Decimal

import pytest

from .conftest import fake_cid
from vorq._crypto import SealedBoxCipher, seal_to
from vorq._results import (
    EmbeddingResult,
    MediaResult,
    TextResult,
    _result_from_output,
    open_result_bytes,
    result_from_raw,
)
from vorq.errors import ResultIntegrityError, VorqError


def settled(job):
    """Read a job's result the only way a settled job is ever read: from the
    bytes it named. The fixtures carry the body as ``output`` for readability;
    this files them under their CID so the dispatch tests run the real path."""
    raw = json.dumps(job["output"]).encode()
    return result_from_raw(raw, {**job, "result_cid": fake_cid(raw)})


def text_job(status="completed"):
    return {
        "id": "job_txt",
        "object": "job",
        "model": "deepseek-ai/deepseek-v4-pro:fp8",
        "status": status,
        "output": {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "deepseek-ai/deepseek-v4-pro:fp8",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            ],
            "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        },
        "vorq": {"gas_fee": "0.03", "fee": "0.000001",
                 "sla_secs": 3600, "rate_in": "0.05", "rate_out": "0.15", "provider_id": 42},
    }


def chat_completion_job(status="completed"):
    # A native job settled through the chat-completions preset: ``output`` is a
    # verbatim ``chat.completion`` object (``choices`` + ``prompt/completion``
    # token usage), passed through by the ``/v1/jobs`` surface as-is.
    return {
        "id": "job_chat",
        "object": "job",
        "model": "deepseek-ai/deepseek-v4-pro:fp8",
        "status": status,
        "output": {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "The capital of France is Paris."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 18, "completion_tokens": 8, "total_tokens": 26},
        },
        "vorq": {"gas_fee": "0.03", "fee": "0",
                 "sla_secs": 3600, "rate_in": "0.22", "rate_out": "0.75", "provider_id": 42},
    }


IMAGE_FRAME = b"\x89PNG\r\n\x1a\n fake pixels"


def image_job():
    return {
        "id": "job_img",
        "object": "job",
        "model": "black-forest-labs/flux-2-dev:fp8",
        "status": "completed",
        "queue_position": None,
        "created_at": 1752600100,
        "completed_at": 1752600118,
        "metadata": {},
        "output": {
            "images": [
                {"b64": base64.b64encode(IMAGE_FRAME).decode(), "content_type": "image/png",
                 "width": 1024, "height": 768}
            ],
            "seed": 42,
        },
        "metrics": {"inference_time": 12.4},
        # The emulator OMITS rate_in for models that meter no input side
        # (guide.md §2), rather than sending it as null. Parser uses .get().
        "vorq": {"gas_fee": "0.03", "fee": "0.000157", "sla_secs": 86400, "rate_out": "0.02", "provider_id": 42},
    }


class TestTextResult:
    def test_flattens_text_and_normalizes_usage(self):
        r = settled(text_job())
        assert isinstance(r, TextResult)
        assert r.text == "Hello world"
        assert r.usage == {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
        assert r.output == text_job()["output"]["output"]
        assert r.raw == text_job()["output"]

    def test_carries_rates_provider_and_job_id(self):
        r = settled(text_job())
        assert r.rates == (Decimal("0.05"), Decimal("0.15"))
        assert r.provider == 42
        assert r.job_id == "job_txt"
        assert r.custom_id is None

    def test_carries_the_gas_fee_the_job_was_posted_under(self):
        r = settled(text_job())
        assert r.gas_fee == Decimal("0.03")
        assert r.cost == "0.000125"      # the fee is stated beside the cost, never inside it

    def test_carries_the_protocol_fee_settlement_took(self):
        r = settled(text_job())
        assert r.fee == Decimal("0.000001")
        assert r.cost == "0.000125"      # stated beside the cost, never inside it

    def test_cost_is_signed_rates_times_token_counts_over_1e6(self):
        # (1000*0.05 + 500*0.15) / 1e6 = (50 + 75)/1e6 = 0.000125
        r = settled(text_job())
        assert r.cost == "0.000125"

    def test_parses_verbatim_chat_completion_job_output(self):
        # A job settled via the chat-completions preset returns a chat.completion
        # object (choices + prompt/completion tokens), not the Responses shape.
        r = settled(chat_completion_job())
        assert isinstance(r, TextResult)
        assert r.text == "The capital of France is Paris."
        assert r.usage == {"input_tokens": 18, "output_tokens": 8, "total_tokens": 26}
        # (18*0.22 + 8*0.75)/1e6 = (3.96 + 6.0)/1e6 = 0.00000996
        assert r.cost == "0.00000996"


class TestMediaResult:
    def test_frames_seed_and_raw(self):
        r = settled(image_job())
        assert isinstance(r, MediaResult)
        assert r.frames == image_job()["output"]["images"]
        assert r.bytes() == [IMAGE_FRAME]
        assert r.seed == 42
        assert r.raw == image_job()["output"]

    def test_media_cost_is_output_pixels_times_rate_out(self):
        # 1 image of 1024×768 = 786_432 px × 0.02 / 1e6 = 0.01572864
        r = settled(image_job())
        assert r.cost == "0.01572864"
        assert r.rates == (None, Decimal("0.02"))

    def test_a_result_that_states_its_settled_units_is_costed_on_them(self):
        """Frames are labelled with what was delivered, and the order's `units_out`
        caps what is charged. When a render comes back larger than its cap the two
        differ, and the charge is the smaller — which only the provider's stamp says.
        """
        job = image_job()
        job["output"]["units"] = 500_000          # capped under the frame's 786_432 px
        r = settled(job)
        assert r.cost == "0.01"

    def test_video_frames_and_cost_from_pixel_seconds(self):
        job = {
            "id": "job_vid",
            "object": "job",
            "model": "wan-ai/wan-2-6:fp8",
            "status": "completed",
            "output": {
                "video": {"b64": base64.b64encode(b"MP4DATA").decode(),
                          "content_type": "video/mp4",
                          "width": 1024, "height": 1024, "duration_secs": 5},
                "seed": 7,
            },
            "vorq": {"gas_fee": "0.03", "fee": "0.001048",
                     "sla_secs": 86400, "rate_in": None, "rate_out": "0.02", "provider_id": 7},
        }
        r = settled(job)
        assert isinstance(r, MediaResult)
        assert r.frames == [job["output"]["video"]]
        assert r.bytes() == [b"MP4DATA"]
        # 1024×1024 × 5s = 5_242_880 pixel-seconds × 0.02 / 1e6 = 0.1048576
        assert r.cost == "0.1048576"

    def test_an_unreported_seed_is_absent_not_null(self):
        job = image_job()
        del job["output"]["seed"]
        assert settled(job).seed is None


class TestCorrelationStamp:
    """The provider stamps `{job_id, custom_id}` onto every sealed result. The client reads
    it onto the result and leaves `raw` as the model's own object."""

    def _stamped(self, vorq: dict) -> dict:
        return {
            "id": "resp_1", "object": "response", "status": "completed",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "vorq": vorq,
        }

    def test_custom_id_from_the_stamp_lands_on_the_result(self):
        output = self._stamped({"job_id": "job_1", "custom_id": "req-42"})
        r = _result_from_output(
            output, {"id": "job_1", "vorq": {"rate_in": "0", "rate_out": "0", "gas_fee": "0.03", "fee": "0"}}
        )
        assert r.custom_id == "req-42"

    def test_the_stamp_is_not_part_of_the_models_own_object(self):
        """`raw` is what the model returned. The stamp is VORQ's, added outside it, and a
        caller reading `raw` must see exactly the OpenAI shape it would have got directly."""
        output = self._stamped({"job_id": "job_1", "custom_id": "req-42"})
        r = _result_from_output(output, {"id": "job_1", "vorq": {"gas_fee": "0.03", "fee": "0"}})
        assert "vorq" not in r.raw
        assert r.raw["object"] == "response"

    def test_an_unstamped_result_still_reads(self):
        """Nothing requires the stamp: a result sealed before this existed, or by a daemon
        that does not stamp, is an ordinary result with no label."""
        output = self._stamped({})
        del output["vorq"]
        r = _result_from_output(output, {"id": "job_1", "vorq": {"gas_fee": "0.03", "fee": "0"}})
        assert r.custom_id is None

    def test_a_stamp_naming_another_job_is_never_an_error(self):
        """Deliberately NOT a refusal. The client has already paid for these bytes; throwing
        them away over a label would turn a provider-side bookkeeping bug into a lost answer.
        The result reads normally and `job_id` stays the job the client asked about."""
        output = self._stamped({"job_id": "some-other-job", "custom_id": "req-9"})
        r = _result_from_output(output, {"id": "job_1", "vorq": {"gas_fee": "0.03", "fee": "0"}})
        assert r.custom_id == "req-9"
        assert r.job_id == "job_1"
        assert r.text == "hi"


def test_an_embedding_response_dispatches_to_an_embedding_result():
    """The provider seals the OpenAI `EmbeddingResponse` verbatim, so the client opens exactly
    that. It is neither a Responses object nor a chat completion, and falling through to either
    would silently produce an empty `TextResult`."""
    output = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": "dmVjLTA="},
            {"object": "embedding", "index": 1, "embedding": "dmVjLTE="},
        ],
        "model": "org/embed:fp8",
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }
    result = _result_from_output(
        output, {"id": "job_1", "vorq": {"rate_in": "0.02", "rate_out": "0", "gas_fee": "0.03", "fee": "0"}}
    )

    assert isinstance(result, EmbeddingResult)
    assert result.model == "org/embed:fp8"
    assert result.prompt_tokens == 12
    assert len(result.embeddings) == 2
    assert result.job_id == "job_1"


def test_an_embedding_result_costs_only_its_input_side():
    """There is no output leg to bill: the backend reports no completion count and the job
    settles at `completionTok == 0`. A cost that read `rate_out` would invent a charge the
    chain never made."""
    output = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": "dmVj"}],
        "model": "m",
        "usage": {"prompt_tokens": 1_000_000, "total_tokens": 1_000_000},
    }
    # A nonzero rate_out is deliberately present — an input-only ask may still carry one, and
    # it must not reach the bill.
    result = _result_from_output(
        output, {"id": "j", "vorq": {"rate_in": "0.02", "rate_out": "9.99", "gas_fee": "0.03", "fee": "0.0002"}}
    )

    assert result.cost == "0.02"


def test_an_embedding_result_decodes_its_vectors():
    output = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": base64.b64encode(b"\x01\x02\x03").decode()}],
        "model": "m",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    result = _result_from_output(output, {"id": "j", "vorq": {"rate_in": "0.02", "gas_fee": "0.03", "fee": "0"}})

    assert result.bytes() == [b"\x01\x02\x03"]


def test_media_result_decodes_frames_without_a_network_call(tmp_path):
    """The frames arrived inside the result, so reading them is a decode."""
    frame = b"\x89PNG\r\n\x1a\n fake pixels"
    output = {
        "images": [{"b64": base64.b64encode(frame).decode(),
                    "content_type": "image/png", "width": 1024, "height": 768}],
        "seed": 42,
    }
    result = _result_from_output(
        output, {"id": "job_1", "vorq": {"rate_out": "0.09", "gas_fee": "0.03", "fee": "0.000707"}}
    )

    assert isinstance(result, MediaResult)
    assert result.seed == 42
    assert result.frames == [{"b64": output["images"][0]["b64"], "content_type": "image/png",
                              "width": 1024, "height": 768}]
    assert result.bytes() == [frame]

    paths = result.download(tmp_path)
    assert [p.read_bytes() for p in paths] == [frame]
    assert paths[0].suffix == ".png"


def test_media_cost_still_bills_pixels():
    output = {"images": [{"b64": "", "content_type": "image/png", "width": 1024, "height": 768},
                         {"b64": "", "content_type": "image/png", "width": 1024, "height": 768}]}
    result = _result_from_output(
        output, {"id": "job_1", "vorq": {"rate_out": "0.09", "gas_fee": "0.03", "fee": "0.001415"}}
    )
    # 2 × 1024 × 768 = 1_572_864 output pixels × 0.09 / 1e6 = 0.14155776
    assert result.cost == "0.14155776"


def test_video_result_reads_duration():
    output = {"video": {"b64": base64.b64encode(b"mp4").decode(),
                        "content_type": "video/mp4", "duration_secs": 5}}
    result = _result_from_output(
        output, {"id": "job_1", "vorq": {"rate_out": "0.09", "gas_fee": "0.03", "fee": "0"}}
    )
    assert result.bytes() == [b"mp4"]
    assert result.frames[0]["duration_secs"] == 5


PLAINTEXT_OUTPUT = {
    "id": "resp_sealed",
    "object": "response",
    "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "sealed hi"}]}
    ],
    "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
}


def sealed_result_bytes(cipher: SealedBoxCipher) -> bytes:
    """The bytes a provider settles: the sealed envelope, serialized to JSON."""
    sealed = seal_to(cipher.public_key, json.dumps(PLAINTEXT_OUTPUT).encode())
    return json.dumps(
        {"enc": "vorq-sealed-v1", "ciphertext": base64.b64encode(sealed).decode()}
    ).encode()


class TestOpenResultBytes:
    def test_opens_matching_bytes(self):
        cipher = SealedBoxCipher.generate()
        raw = sealed_result_bytes(cipher)
        assert open_result_bytes(raw, fake_cid(raw), cipher) == PLAINTEXT_OUTPUT

    def test_passes_cleartext_bytes_through(self):
        raw = json.dumps(PLAINTEXT_OUTPUT).encode()
        assert open_result_bytes(raw, fake_cid(raw), None) == PLAINTEXT_OUTPUT

    def test_sealed_bytes_without_a_cipher_raise(self):
        cipher = SealedBoxCipher.generate()
        raw = sealed_result_bytes(cipher)
        with pytest.raises(ValueError, match="no cipher"):
            open_result_bytes(raw, fake_cid(raw), None)

    def test_every_way_the_open_can_fail_is_one_shaped_error(self):
        """Unreadable bytes, a missing cipher and a wrong cipher are one class.

        They are three different mechanics and one answer to the caller: this
        job has no result to hand back. A `nacl` exception escaping here would
        be the only member of the set a caller holding `except VorqError` would
        miss, and it is the one that happens in production.
        """
        cipher = SealedBoxCipher.generate()
        raw = sealed_result_bytes(cipher)
        with pytest.raises(ResultIntegrityError):     # no cipher at all
            open_result_bytes(raw, fake_cid(raw), None)
        with pytest.raises(ResultIntegrityError):     # somebody else's cipher
            open_result_bytes(raw, fake_cid(raw), SealedBoxCipher.generate())
        with pytest.raises(ResultIntegrityError):     # not a result object
            open_result_bytes(b"\x00\x01", fake_cid(b"\x00\x01"), cipher)


class TestResultFromRaw:
    def test_builds_the_same_result_as_the_inline_output_path(self):
        cipher = SealedBoxCipher.generate()
        raw = sealed_result_bytes(cipher)
        job = {
            "id": "job_cid",
            "object": "job",
            "status": "completed",
            "result_cid": fake_cid(raw),
            "vorq": {"gas_fee": "0.03", "fee": "0",
                     "sla_secs": 3600, "rate_in": "0.05", "rate_out": "0.15", "provider_id": 42},
        }
        r = result_from_raw(raw, job, cipher=cipher)
        assert isinstance(r, TextResult)
        assert r.text == "sealed hi"
        assert r.rates == (Decimal("0.05"), Decimal("0.15"))
        assert r.provider == 42
        assert r.job_id == "job_cid"
        # (10*0.05 + 20*0.15)/1e6 = 3.5/1e6
        assert r.cost == "0.0000035"

    def test_media_output_still_dispatches_to_mediaresult(self):
        raw = json.dumps(
            {"images": [{"b64": base64.b64encode(IMAGE_FRAME).decode(),
                         "content_type": "image/png", "width": 1024, "height": 768}],
             "seed": 42}
        ).encode()
        job = {
            "id": "job_img",
            "status": "completed",
            "result_cid": fake_cid(raw),
            "vorq": {"gas_fee": "0.03", "fee": "0.000157",
                     "sla_secs": 86400, "rate_out": "0.02", "provider_id": 42},
        }
        r = result_from_raw(raw, job, cipher=None)
        assert isinstance(r, MediaResult)
        assert r.bytes() == [IMAGE_FRAME]
        # 1024×768 = 786_432 px × 0.02 / 1e6 = 0.01572864
        assert r.cost == "0.01572864"


class TestOpaqueBytes:
    """Bytes fetched under a result name that are not a result object."""

    def test_non_json_bytes_raise_a_result_shaped_error(self):
        raw = b"\x00\x01\xff\xfe"
        with pytest.raises(ResultIntegrityError, match="not JSON"):
            open_result_bytes(raw, fake_cid(raw), None)

    def test_json_that_is_not_an_object_raises_a_result_shaped_error(self):
        raw = json.dumps([1, 2, 3]).encode()
        with pytest.raises(ResultIntegrityError, match="not a result object"):
            open_result_bytes(raw, fake_cid(raw), None)

    def test_the_error_is_both_a_vorqerror_and_a_valueerror(self):
        raw = b"\x00\x01\xff\xfe"
        with pytest.raises(VorqError):
            open_result_bytes(raw, fake_cid(raw), None)
        with pytest.raises(ValueError):
            open_result_bytes(raw, fake_cid(raw), None)

    def test_it_reaches_the_caller_through_result_from_raw(self):
        raw = b"\x00\x01\xff\xfe"
        job = {"id": "job_opaque", "status": "completed",
               "result_cid": fake_cid(raw), "vorq": {"gas_fee": "0.03", "fee": "0"}}
        with pytest.raises(ResultIntegrityError):
            result_from_raw(raw, job)


def test_a_success_row_that_names_no_result_is_refused_rather_than_invented():
    """The batch line reader is back, and it fails closed.

    Every output row names its bytes and carries `response.body: null` — the
    result is sealed to this client's own key, so the coordinator cannot read it
    and does not pretend to. A row that claims a delivery and names nothing has
    nothing that can be checked, and returning an empty result for it would be the
    one way sealed bodies could be swapped between lines.
    """
    import vorq
    from vorq._results import JobError, result_from_batch_line

    with pytest.raises(ResultIntegrityError):
        result_from_batch_line({"id": "batch_req_1_1", "response": {"body": None}, "vorq": {}})

    # An error row needs no bytes at all: it carries its own cause.
    parsed = result_from_batch_line(
        {"id": "batch_req_1_2", "error": {"code": "reclaim", "message": "no settle"},
         "vorq": {"job_id": "0xabc"}}
    )
    assert isinstance(parsed, JobError)
    assert (parsed.type, parsed.job_id) == ("reclaim", "0xabc")
    assert "JobError" in vorq.__all__


class TestFramesThatAreNotFrames:
    """A declared frame that carries no decodable bytes is a refusal, not an empty file.

    `base64.b64decode` without `validate=True` **skips** characters outside the
    base64 alphabet and returns whatever is left, so `b64decode("###")` is `b""`.
    A provider that declares 1024x768 and seals garbage therefore produced a
    result that read as a perfectly good zero-byte image, and `download()` wrote
    that zero-byte image to disk. The three cases below are the three shapes that
    failure takes, and all three mean the same thing to a caller — this job has
    no frames to hand back — so all three raise the one error every unreadable
    result already raises.
    """

    def test_a_frame_that_is_not_base64_raises_instead_of_yielding_an_empty_file(self):
        job = image_job()
        job["output"]["images"][0]["b64"] = "###"
        with pytest.raises(ResultIntegrityError) as excinfo:
            settled(job).bytes()
        assert "frame 0" in str(excinfo.value)
        assert "not base64" in str(excinfo.value)

    def test_a_frame_with_no_b64_member_raises_rather_than_a_bare_keyerror(self):
        # A KeyError is not a VorqError, so it walks straight through the
        # `except VorqError` a caller holds over the whole read path.
        job = image_job()
        del job["output"]["images"][0]["b64"]
        with pytest.raises(ResultIntegrityError) as excinfo:
            settled(job).bytes()
        assert "carries no base64" in str(excinfo.value)

    def test_a_video_frame_is_held_to_the_same_rule(self):
        job = {
            "id": "job_vid",
            "object": "job",
            "model": "wan-ai/wan-2-6:fp8",
            "status": "completed",
            "output": {"video": {"b64": "not base64 at all!", "content_type": "video/mp4",
                                 "width": 1024, "height": 1024, "duration_secs": 5}},
            "vorq": {"gas_fee": "0.03", "fee": "0.001048",
                     "sla_secs": 86400, "rate_out": "0.02", "provider_id": 7},
        }
        with pytest.raises(ResultIntegrityError):
            settled(job).bytes()

    def test_the_refusal_is_the_same_shaped_error_every_unreadable_result_raises(self):
        job = image_job()
        job["output"]["images"][0]["b64"] = "###"
        result = settled(job)
        with pytest.raises(VorqError):
            result.bytes()
        with pytest.raises(ValueError):
            result.bytes()

    def test_download_refuses_before_it_writes_an_empty_file(self, tmp_path):
        # `download` decodes every frame first and writes second, so the refusal
        # lands before any path exists — a half-written directory of empty files
        # would be worse than the silence this task removes.
        job = image_job()
        job["output"]["images"][0]["b64"] = "###"
        with pytest.raises(ResultIntegrityError):
            settled(job).download(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_an_empty_frame_that_really_is_empty_still_decodes(self):
        # `""` is valid base64 for zero bytes. The rule is "not decodable",
        # never "suspiciously small": a model that legitimately produced nothing
        # is a different conversation and not this one.
        job = image_job()
        job["output"]["images"][0]["b64"] = ""
        assert settled(job).bytes() == [b""]


def test_media_cost_uses_the_same_rate_scale_as_the_chain():
    """A rate is USD per 1M units, and the displayed cost is units × rate / 1e6.

    At 6 decimals the same rate is the on-chain atomic rate, and
    `JobRegistry._atomicCharge` charges ceilDiv(rateOut*unitsOut, RATE_SCALE)
    atomic units. Pinned against the chain's own arithmetic so the two cannot
    drift apart.
    """
    RATE_SCALE = 1_000_000
    px = 1024 * 768
    output = {"images": [{"b64": "", "content_type": "image/png",
                          "width": 1024, "height": 768}]}
    result = _result_from_output(
        output, {"id": "job_px", "vorq": {"rate_in": None, "rate_out": "0.000002", "gas_fee": "0.03", "fee": "0"}}
    )
    # What the chain actually charges: ceilDiv(rateOut*unitsOut, RATE_SCALE), rateOut = 2.
    charged = max(1, -(-(2 * px) // RATE_SCALE))
    assert charged == 2
    # The display is the unrounded USD form of that same product.
    assert result.cost == "0.000001572864"
    assert Decimal(result.cost) * 10**6 == Decimal(2 * px) / RATE_SCALE


def test_cost_is_exact_past_the_default_decimal_precision():
    """30 significant digits of product: the default 28-digit context would round it."""
    output = {"images": [{"b64": "", "content_type": "image/png", "width": 1, "height": 1}]}
    result = _result_from_output(
        output, {"id": "j", "vorq": {"rate_out": "123456789012345678901234.567891",
                                     "gas_fee": "0.03", "fee": "1234567890123456.789012"}}
    )
    assert result.cost == "123456789012345678.901234567891"


@pytest.mark.parametrize("rate", ["0.050", "1e3", "-1", " 1", 50000, 0.5, True])
def test_a_rate_that_is_not_a_canonical_usd_string_is_refused(rate):
    """Every rate on the wire is a canonical USD string; anything else is the node's fault."""
    output = {"images": [{"b64": "", "content_type": "image/png", "width": 1, "height": 1}]}
    with pytest.raises(VorqError) as excinfo:
        _result_from_output(output, {"id": "j", "vorq": {"rate_out": rate, "gas_fee": "0.03", "fee": "0"}})
    assert excinfo.value.type == "api_error"


@pytest.mark.parametrize("gas_fee", [None, "0.030", "1e3", "-1", 30000, 0.03])
def test_a_job_row_whose_gas_fee_is_missing_or_not_a_canonical_usd_string_is_refused(gas_fee):
    """`vorq.gas_fee` is required on every job row, held to the same grammar as its rates."""
    job = text_job()
    job["vorq"]["gas_fee"] = gas_fee
    if gas_fee is None:
        del job["vorq"]["gas_fee"]
    with pytest.raises(VorqError) as excinfo:
        settled(job)
    assert excinfo.value.type == "api_error"
    assert "vorq.gas_fee" in str(excinfo.value)


@pytest.mark.parametrize("fee", [None, "0.0000010", "1e3", "-1", 1, 0.000001])
def test_a_job_row_whose_fee_is_missing_or_not_a_canonical_usd_string_is_refused(fee):
    """`vorq.fee` is required on every job row, held to the same grammar as `gas_fee`."""
    job = text_job()
    job["vorq"]["fee"] = fee
    if fee is None:
        del job["vorq"]["fee"]
    with pytest.raises(VorqError) as excinfo:
        settled(job)
    assert excinfo.value.type == "api_error"
    assert "vorq.fee" in str(excinfo.value)


def _batch_success_row(raw: bytes, **over) -> dict:
    vorq = {"job_id": "0xabc", "result_cid": fake_cid(raw), "provider": 7,
            "rate_in": "0.05", "rate_out": "0.15", "gas_fee": "0.03", "fee": "0", **over}
    return {"id": "batch_req_1_1", "response": {"body": None}, "vorq": vorq}


def test_a_batch_line_result_carries_its_gas_fee_and_fee():
    from vorq._results import result_from_batch_line

    raw = json.dumps(PLAINTEXT_OUTPUT).encode()
    r = result_from_batch_line(_batch_success_row(raw), raw=raw)
    assert isinstance(r, TextResult)
    assert r.gas_fee == Decimal("0.03")
    assert r.fee == 0
    assert r.rates == (Decimal("0.05"), Decimal("0.15"))


@pytest.mark.parametrize("gas_fee", [None, "0.030", "1e3", 30000])
def test_a_batch_success_row_whose_gas_fee_is_missing_or_not_canonical_is_refused(gas_fee):
    """A batch success row is held to the job row's rule: `vorq.gas_fee` is required."""
    from vorq._results import result_from_batch_line

    raw = json.dumps(PLAINTEXT_OUTPUT).encode()
    row = _batch_success_row(raw, gas_fee=gas_fee)
    if gas_fee is None:
        del row["vorq"]["gas_fee"]
    with pytest.raises(VorqError) as excinfo:
        result_from_batch_line(row, raw=raw)
    assert excinfo.value.type == "api_error"
    assert "vorq.gas_fee" in str(excinfo.value)


@pytest.mark.parametrize("fee", [None, "0.0000010", "1e3", 1])
def test_a_batch_success_row_whose_fee_is_missing_or_not_canonical_is_refused(fee):
    """A batch success row is held to the job row's rule: `vorq.fee` is required."""
    from vorq._results import result_from_batch_line

    raw = json.dumps(PLAINTEXT_OUTPUT).encode()
    row = _batch_success_row(raw, fee=fee)
    if fee is None:
        del row["vorq"]["fee"]
    with pytest.raises(VorqError) as excinfo:
        result_from_batch_line(row, raw=raw)
    assert excinfo.value.type == "api_error"
    assert "vorq.fee" in str(excinfo.value)
