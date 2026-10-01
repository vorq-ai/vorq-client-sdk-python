"""The batch surface, checked against the **real** ``openai`` package.

Every object below is transcribed from the coordinator's own serializers —
`src/batches/object.ts` and `src/api/routes/files.ts` — and handed to OpenAI's own
pydantic models. That is the whole point: a hand-written assertion about "the
OpenAI shape" is a restatement of what the author believed, and the thing a caller
actually runs is `openai.types.Batch.model_validate`. The package is the authority
and it is already a dev dependency, so asking it costs nothing.

It has earned its place twice already. Two shapes were about to ship that read
correctly against OpenAI's published spec and **raise** against their package:

  * `request_counts` as ``{processing, succeeded, errored}`` — three places in
    OpenAI's own spec agree on those names, and `BatchRequestCounts` declares
    ``completed``, ``failed`` and ``total``, all required.
  * a file whose ``purpose`` is ``batch_error`` — not a member of
    `FileObject.purpose`'s Literal, so the object fails to parse at all.

Neither is visible to a test that writes down what it expects.
"""

from __future__ import annotations

import pytest
from openai.types import Batch, FileObject

# The additive block VORQ hangs off every object. Stock tooling ignores it; these
# tests assert that it does not stop the object parsing.
VORQ_BATCH_EXTRA = {"sla": "24h"}


def coordinator_batch(**over) -> dict:
    """`batchObject(...)` — key for key, from `src/batches/object.ts`."""
    body = {
        "id": "batch_9f2c",
        "object": "batch",
        "endpoint": "/v1/responses",
        "input_file_id": "file-in",
        "completion_window": "24h",
        "status": "completed",
        "errors": None,
        "output_file_id": "file-out",
        "error_file_id": "file-err",
        "created_at": 1_800_000_000,
        "expires_at": 1_800_086_400,
        "in_progress_at": 1_800_000_030,
        "finalizing_at": 1_800_000_900,
        "completed_at": 1_800_000_901,
        "failed_at": None,
        "expired_at": None,
        "cancelling_at": None,
        "cancelled_at": None,
        "request_counts": {"completed": 2, "failed": 1, "total": 3},
        "metadata": {"run": "nightly"},
        "vorq": VORQ_BATCH_EXTRA,
    }
    body.update(over)
    return body


def coordinator_file(**over) -> dict:
    """`FileObject` — key for key, from `src/api/routes/files.ts`."""
    body = {
        "id": "file-4a1b",
        "object": "file",
        "bytes": 4096,
        "created_at": 1_800_000_000,
        "expires_at": None,
        "filename": "batch.jsonl",
        "purpose": "batch",
        "status": "uploaded",
        "vorq": {"cid": "bafyinput", "lines": 3},
    }
    body.update(over)
    return body


class TestBatchObject:
    def test_the_coordinator_s_batch_object_parses_as_openai_s_own(self):
        batch = Batch.model_validate(coordinator_batch())

        assert batch.id == "batch_9f2c"
        assert batch.status == "completed"
        assert batch.request_counts is not None
        # The three attribute names stock code reads.
        assert (
            batch.request_counts.completed,
            batch.request_counts.failed,
            batch.request_counts.total,
        ) == (2, 1, 3)

    def test_the_documented_counts_shape_would_not_parse(self):
        """The finding, kept as a test so it cannot come back.

        OpenAI's published spec, its batch reference example and its Python
        quick-reference all describe `{processing, succeeded, errored}`. Their
        package requires the other three, so emitting the documented shape is not
        a lossy render — it is an exception in every stock caller's parse.
        """
        with pytest.raises(Exception):
            Batch.model_validate(
                coordinator_batch(request_counts={"processing": 1, "succeeded": 2, "errored": 0})
            )

    @pytest.mark.parametrize(
        "status",
        ["validating", "in_progress", "finalizing", "completed",
         "failed", "expired", "cancelling", "cancelled"],
    )
    def test_every_status_this_node_can_answer_is_one_openai_knows(self, status):
        """`src/batches/fold.ts` names eight; the package has to accept all eight."""
        assert Batch.model_validate(coordinator_batch(status=status)).status == status

    @pytest.mark.parametrize("window", ["1h", "24h"])
    def test_both_completion_windows_parse_including_the_one_openai_dropped(self, window):
        """`1h` is a deliberate superset: the window **is** the per-line SLA this
        network signs, and an hour is a real SLA a provider quotes. A client
        written against OpenAI sends `24h` and is unaffected."""
        assert Batch.model_validate(coordinator_batch(completion_window=window))

    def test_a_validating_batch_parses_with_every_stamp_still_null(self):
        """The shape at create time: no files, no counts to speak of, no stamps."""
        batch = Batch.model_validate(
            coordinator_batch(
                status="validating",
                output_file_id=None,
                error_file_id=None,
                in_progress_at=None,
                finalizing_at=None,
                completed_at=None,
                request_counts={"completed": 0, "failed": 0, "total": 0},
            )
        )
        assert batch.output_file_id is None

    def test_the_vorq_block_survives_the_parse_rather_than_breaking_it(self):
        """Additive, under one key, and ignored by stock tooling.

        Asserting it *survives* rather than merely that it is tolerated: the
        openai models allow extras, so a caller that wants the SLA back can read
        it off the parsed object instead of the raw body.
        """
        batch = Batch.model_validate(coordinator_batch())
        assert getattr(batch, "vorq", None) == VORQ_BATCH_EXTRA


class TestFileObject:
    @pytest.mark.parametrize("purpose", ["batch", "batch_output"])
    def test_both_purposes_this_node_mints_are_ones_openai_knows(self, purpose):
        """`batch` is uploaded and `batch_output` is minted at finalization.

        There is no third: the error file is `batch_output` too. `batch_error`
        reads like the obvious name and is not in OpenAI's Literal at all — the
        two files are told apart by which field of the batch names them.
        """
        assert FileObject.model_validate(coordinator_file(purpose=purpose)).purpose == purpose

    def test_batch_error_is_not_a_purpose_openai_accepts(self):
        with pytest.raises(Exception):
            FileObject.model_validate(coordinator_file(purpose="batch_error"))

    @pytest.mark.parametrize("status", ["uploaded", "processed", "error"])
    def test_every_status_this_node_emits_parses(self, status):
        """OpenAI deprecated `status` on their side and still emits it, so this node
        does too — deprecated is not absent, and a client reading it must not find
        a value the model refuses."""
        assert FileObject.model_validate(coordinator_file(status=status)).status == status

    def test_expires_at_is_emitted_as_null_rather_than_omitted(self):
        """A pinned object has no expiry, and the field is still there.

        Emitting `null` rather than dropping the key is the difference between
        "this file does not expire" and "this node does not say", and only the
        first is true.
        """
        assert "expires_at" in coordinator_file()
        assert FileObject.model_validate(coordinator_file()).expires_at is None
