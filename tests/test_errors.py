import pytest

from vorq.errors import (
    AuthenticationError,
    EscrowKeyUnverified,
    JobFailed,
    NotFoundError,
    ResultIntegrityError,
    StateConflictError,
    ValidationError,
    VerificationError,
    VorqError,
    WaitTimeout,
    error_from_wire,
)


class TestHierarchy:
    def test_every_error_descends_from_vorqerror(self):
        for cls in (
            AuthenticationError,
            NotFoundError,
            StateConflictError,
            ValidationError,
            WaitTimeout,
            JobFailed,
            ResultIntegrityError,
            VerificationError,
            EscrowKeyUnverified,
        ):
            assert issubclass(cls, VorqError)


class TestErrorFromWire:
    def _err(self, status, type_="some_error", code=None):
        body = {"error": {"message": "boom", "type": type_, "param": None, "code": code}}
        return error_from_wire(status, body, request_id="req_123")

    def test_maps_status_codes_to_classes(self):
        assert isinstance(self._err(401, "authentication_error"), AuthenticationError)
        assert isinstance(self._err(404, "not_found"), NotFoundError)
        assert isinstance(self._err(409, "state_conflict"), StateConflictError)
        assert isinstance(self._err(400, "invalid_request_error"), ValidationError)

    def test_unmapped_status_is_base_vorqerror(self):
        err = self._err(500, "internal_error")
        assert type(err) is VorqError

    def test_carries_type_message_and_request_id(self):
        err = self._err(400, "invalid_request_error")
        assert err.type == "invalid_request_error"
        assert err.request_id == "req_123"
        assert "boom" in str(err)

    def test_tolerates_missing_error_envelope(self):
        err = error_from_wire(500, {}, request_id=None)
        assert isinstance(err, VorqError)
        assert err.request_id is None


class TestWaitTimeout:
    def test_carries_job_id(self):
        err = WaitTimeout("timed out", job_id="job_9")
        assert err.job_id == "job_9"

    def test_is_raisable(self):
        with pytest.raises(WaitTimeout):
            raise WaitTimeout("x", job_id="job_1")


class TestJobFailed:
    def test_carries_error_type_and_job_id(self):
        err = JobFailed("job failed", error_type="reclaim", job_id="job_5")
        assert err.error_type == "reclaim"
        assert err.job_id == "job_5"
        assert err.type == "reclaim"


def test_escrow_key_unverified_is_a_verification_error_and_is_exported():
    """It lives in `vorq.errors` and is re-exported, like every other error here.

    A caller already holding `except VerificationError` keeps catching it, which
    is what makes fail-closed the *stronger* behaviour rather than a new
    exception nobody handles.
    """
    import vorq
    from vorq.errors import EscrowKeyUnverified, VerificationError, VorqError

    assert issubclass(EscrowKeyUnverified, VerificationError)
    assert issubclass(EscrowKeyUnverified, VorqError)
    assert vorq.EscrowKeyUnverified is EscrowKeyUnverified
    assert "EscrowKeyUnverified" in vorq.__all__


def test_a_failed_batch_is_the_input_file_and_never_a_line():
    """`BatchFailed` is back, and it names exactly one thing.

    It was removed when batches were gated — an exception nothing can raise reads
    as a surface that is there — and it returns with them. What it must not become
    is a per-line failure: a line that did not deliver is a row in the error file
    and reaches a caller as a `JobError`. `BatchFailed` is the *input file* being
    refused, which is a different fact with a different remedy.
    """
    import vorq
    from vorq.errors import BatchFailed

    error = BatchFailed("Batch batch_1 failed.", batch_id="batch_1")
    assert error.batch_id == "batch_1"
    assert error.type == "batch_failed"
    assert "BatchFailed" in vorq.__all__
    assert vorq.BatchFailed is BatchFailed
