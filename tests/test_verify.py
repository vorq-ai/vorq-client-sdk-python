"""Client-edge attestation verification: pass/fail against mock chain state."""

import asyncio
import hashlib

import httpx
import pytest

from vorq.errors import VerificationError
from vorq.verify import Verifier, report_data

MEASUREMENT = hashlib.sha256(b"vorq-mock-cvm-image-v1").hexdigest()
WALLET = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
BOX = "ab" * 32


def evidence(*, measurement=MEASUREMENT, rd=None, debug=False, svn=1, type_="mock-cvm-v1"):
    return {"type": type_, "measurement": measurement,
            "report_data": rd if rd is not None else report_data(BOX, WALLET),
            "debug": debug, "tcb": {"svn": svn}, "quote": "b3BhcXVl"}


def record(ev, provider=7):
    return {"provider": provider, "operator": WALLET, "box_key": BOX, "evidence": ev}


def _verifier(entries, records=None, mode="mock"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": entries, "signature": "0x0", "signer": "0x0"})
        if request.url.path.startswith("/evm/providers/"):
            pid = int(request.url.path.rsplit("/", 1)[1])
            rec = (records or {}).get(pid)
            return httpx.Response(200, json=rec) if rec else httpx.Response(404, json={})
        return httpx.Response(404, json={})

    return Verifier("http://emu", mode=mode, transport=httpx.MockTransport(handler))


ACTIVE = [{"kind": "image", "measurement": MEASUREMENT, "release": "mock-dev", "status": "active", "mock": True}]


async def test_valid_mock_evidence_passes_in_mock_mode():
    await _verifier(ACTIVE).verify_record(record(evidence()))   # no raise


@pytest.mark.parametrize("ev,match", [
    (None, "no attestation evidence"),
    (evidence(type_="unknown-v9"), "evidence type"),
    (evidence(measurement="00" * 32), "allowlist"),
    (evidence(rd="00" * 32), "bind"),
    (evidence(debug=True), "debug"),
    (evidence(svn=0), "TCB"),
])
async def test_structural_refusals(ev, match):
    with pytest.raises(VerificationError, match=match):
        await _verifier(ACTIVE).verify_record(record(ev))


async def test_revoked_measurement_refuses():
    revoked = [{**ACTIVE[0], "status": "revoked"}]
    with pytest.raises(VerificationError, match="revoked"):
        await _verifier(revoked).verify_record(record(evidence()))


async def test_mock_honesty_outside_mock_mode():
    # mock-tagged evidence and mock allowlist entries are categorically refused.
    with pytest.raises(VerificationError, match="mock"):
        await _verifier(ACTIVE, mode="structural").verify_record(record(evidence()))


async def test_mock_evidence_gate_fires_on_a_non_mock_allowlist():
    # Isolates the evidence half of the mock-honesty rule: with a production
    # (non-mock) allowlist entry, only the evidence tag can refuse this.
    prod = [{"kind": "image", "measurement": MEASUREMENT, "release": "prod", "status": "active"}]
    with pytest.raises(VerificationError, match="mock evidence is refused outside mock mode"):
        await _verifier(prod, mode="structural").verify_record(record(evidence()))


OTHER_BOX = "cd" * 32
OTHER_WALLET = "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc"


def other_record(provider=2):
    """A record that verifies fully ON ITS OWN, for a different (key, payee) pair.

    Only the seal-target pin can drop this one: its evidence binds its own key
    and payee, so every other check passes.
    """
    ev = {**evidence(), "report_data": report_data(OTHER_BOX, OTHER_WALLET)}
    return {"provider": provider, "operator": OTHER_WALLET,
            "box_key": OTHER_BOX, "evidence": ev}


async def test_a_record_for_another_key_pair_verifies_on_its_own():
    # guards the test below: provider 2 must fail ONLY the pin, not verification
    await _verifier(ACTIVE).verify_record(other_record())   # no raise


async def test_verify_candidates_filters_and_pins_the_seal_target():
    records = {
        1: record(evidence(), 1),                                  # verifies, box_key matches
        2: other_record(2),                                        # verifies; box_key ≠ record key
        3: record(evidence(debug=True), 3),                        # fails verification → dropped
    }
    v = _verifier(ACTIVE, records)
    kept = await v.verify_candidates([
        {"provider": 1, "box_key": BOX}, {"provider": 2, "box_key": BOX}, {"provider": 3, "box_key": BOX},
    ])
    assert [c["provider"] for c in kept] == [1]


# --- the two spellings of one key -------------------------------------------
#
# The coordinator serves `box_key` as `0x` hex on `/evm/providers/{id}` — every
# `bytes` column on that wire is. A verifier that rejected the prefix failed
# **closed** on records that are entirely honest: `verify_record` raised about a
# correct record, and `verify_candidates` dropped every candidate silently.


async def test_a_0x_prefixed_box_key_is_the_same_key():
    """The binding is over 32 bytes, not over a spelling."""
    ev = {**evidence(), "report_data": report_data("0x" + BOX, WALLET)}
    assert ev["report_data"] == report_data(BOX, WALLET)
    await _verifier(ACTIVE).verify_record({**record(ev), "box_key": "0x" + BOX})   # no raise


async def test_the_seal_target_pin_survives_a_prefix_on_either_side():
    """A bare challenge against a prefixed record is a match, not a silent drop."""
    v = _verifier(ACTIVE, {1: {**record(evidence(), 1), "box_key": "0x" + BOX}})
    kept = await v.verify_candidates([{"provider": 1, "box_key": BOX}])
    assert [c["provider"] for c in kept] == [1]


# --- the payee is `operator`, and there is no `address` on that wire ---------
#
# `verify_record` read `record["address"]`, which `GET /evm/providers/{id}` has
# never carried — the registry's payee is `operatorOf(id)` on chain and
# `operator` on the wire. The binding was therefore computed over `None` and
# every honest record was refused, so no `submit(provider=N, confidential=True)`
# could succeed. Every fixture here spelled it the way the code did, which is why
# 568 unit tests were green through it.


async def test_the_payee_comes_from_operator():
    rec = record(evidence())
    assert "address" not in rec, "the node serves no such key; a fixture must not either"
    await _verifier(ACTIVE).verify_record(rec)   # no raise


async def test_a_record_carrying_only_the_phantom_address_is_refused():
    """Not a synonym: a record without `operator` has no payee to bind."""
    phantom = {"provider": 7, "address": WALLET, "box_key": BOX, "evidence": evidence()}
    with pytest.raises(VerificationError, match="operator"):
        await _verifier(ACTIVE).verify_record(phantom)


# --- protocol constants -----------------------------------------------------


def test_report_data_binds_key_and_payee_byte_exactly():
    expected = hashlib.sha256(
        bytes.fromhex(BOX) + bytes.fromhex(WALLET[2:])
    ).hexdigest()
    assert report_data(BOX, WALLET) == expected
    assert report_data(BOX, WALLET.upper().replace("0X", "0x")) == expected
    assert report_data(BOX, WALLET.removeprefix("0x")) == expected


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        Verifier("http://emu", mode="strict")


@pytest.mark.parametrize("floor", ["1", -1, None, 1.5, True])
def test_bad_tcb_floor_rejected(floor):
    # a floor that cannot be compared would silently disable the TCB check
    with pytest.raises(ValueError, match="min_tcb_svn"):
        Verifier("http://emu", min_tcb_svn=floor)


async def test_allowlist_is_fetched_once_and_cached():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"entries": ACTIVE})

    v = Verifier("http://emu", mode="mock", transport=httpx.MockTransport(handler))
    assert await v.allowlist() == ACTIVE
    assert await v.allowlist() == ACTIVE
    assert calls["n"] == 1
    await v.aclose()


def _ttl_verifier(entries_box, *, ttl=60.0, mode="mock"):
    """A Verifier over mutable chain state with a hand-cranked clock."""
    now = {"t": 1000.0}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"entries": entries_box["entries"]})

    v = Verifier("http://emu", mode=mode, allowlist_ttl_s=ttl,
                 clock=lambda: now["t"], transport=httpx.MockTransport(handler))
    return v, now, calls


async def test_allowlist_is_reread_after_the_ttl_lapses():
    box = {"entries": ACTIVE}
    v, now, calls = _ttl_verifier(box)
    await v.verify_record(record(evidence()))
    now["t"] += 59.0
    await v.verify_record(record(evidence()))
    assert calls["n"] == 1                      # still inside the TTL
    now["t"] += 2.0
    await v.verify_record(record(evidence()))
    assert calls["n"] == 2                      # TTL lapsed → chain state re-read
    await v.aclose()


async def test_revocation_takes_effect_once_the_ttl_lapses():
    box = {"entries": ACTIVE}
    v, now, _ = _ttl_verifier(box, ttl=30.0)
    await v.verify_record(record(evidence()))                 # accepted
    box["entries"] = [{**ACTIVE[0], "status": "revoked"}]     # chain revokes the image
    await v.verify_record(record(evidence()))                 # cached: still accepted
    now["t"] += 30.0
    with pytest.raises(VerificationError, match="revoked"):
        await v.verify_record(record(evidence()))
    await v.aclose()


async def test_refresh_applies_a_revocation_immediately():
    box = {"entries": ACTIVE}
    v, _, calls = _ttl_verifier(box, ttl=3600.0)
    await v.verify_record(record(evidence()))
    box["entries"] = [{**ACTIVE[0], "status": "revoked"}]
    assert await v.refresh() == box["entries"]
    assert calls["n"] == 2
    with pytest.raises(VerificationError, match="revoked"):
        await v.verify_record(record(evidence()))
    await v.aclose()


async def test_invalidate_forces_the_next_check_to_reread():
    box = {"entries": ACTIVE}
    v, _, calls = _ttl_verifier(box, ttl=3600.0)
    await v.verify_record(record(evidence()))
    box["entries"] = [{**ACTIVE[0], "status": "revoked"}]
    v.invalidate()
    with pytest.raises(VerificationError, match="revoked"):
        await v.verify_record(record(evidence()))
    assert calls["n"] == 2
    await v.aclose()


async def test_zero_ttl_rereads_every_time():
    box = {"entries": ACTIVE}
    v, _, calls = _ttl_verifier(box, ttl=0.0)
    await v.verify_record(record(evidence()))
    await v.verify_record(record(evidence()))
    assert calls["n"] == 2
    await v.aclose()


async def test_failed_reread_does_not_silently_extend_the_stale_window():
    state = {"body": {"entries": ACTIVE}, "status": 200}
    now = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(state["status"], json=state["body"])

    v = Verifier("http://emu", mode="mock", allowlist_ttl_s=10.0,
                 clock=lambda: now["t"], transport=httpx.MockTransport(handler))
    await v.verify_record(record(evidence()))
    state["status"] = 503
    now["t"] += 11.0
    with pytest.raises(httpx.HTTPStatusError):          # loud, not stale-accepted
        await v.verify_record(record(evidence()))
    state["status"], state["body"] = 200, {"entries": "malformed"}
    with pytest.raises(VerificationError, match="allowlist"):
        await v.verify_record(record(evidence()))
    await v.aclose()


async def test_invalidation_during_an_in_flight_read_is_not_overwritten():
    """A revocation notice that lands mid-read must not be lost: the answer the
    in-flight read brings back predates it, so it must not be cached (and
    restamped for another full TTL) on top of the invalidation."""
    box = {"entries": ACTIVE}
    calls = {"n": 0}
    started, gate = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            answer = {"entries": box["entries"]}   # chain state as of the read
            started.set()
            await gate.wait()                     # suspend the first read mid-flight
            return httpx.Response(200, json=answer)
        return httpx.Response(200, json={"entries": box["entries"]})

    v = Verifier("http://emu", mode="mock", allowlist_ttl_s=3600.0,
                 transport=httpx.MockTransport(handler))
    first = asyncio.create_task(v.allowlist())
    await started.wait()
    box["entries"] = [{**ACTIVE[0], "status": "revoked"}]   # chain revokes the image
    v.invalidate()                                          # notice arrives mid-read
    gate.set()
    assert await first == ACTIVE                            # that read still answers
    with pytest.raises(VerificationError, match="revoked"):  # but nothing was pinned
        await v.verify_record(record(evidence()))
    assert calls["n"] == 2
    await v.aclose()


async def test_a_backwards_clock_does_not_pin_the_cache():
    """Negative elapsed time counts as expired: a clock that steps backwards
    (an injected clock, a slewed source) must not freeze a stale allowlist."""
    box = {"entries": ACTIVE}
    v, now, calls = _ttl_verifier(box, ttl=60.0)
    await v.verify_record(record(evidence()))
    box["entries"] = [{**ACTIVE[0], "status": "revoked"}]
    now["t"] -= 5000.0
    with pytest.raises(VerificationError, match="revoked"):
        await v.verify_record(record(evidence()))
    assert calls["n"] == 2
    await v.aclose()


@pytest.mark.parametrize("clock", [None, "now", 123, 1.0])
def test_non_callable_clock_rejected(clock):
    with pytest.raises(ValueError, match="clock"):
        Verifier("http://emu", clock=clock)


@pytest.mark.parametrize("ttl", ["60", -1, None, float("nan"), float("inf"), True])
def test_bad_allowlist_ttl_rejected(ttl):
    with pytest.raises(ValueError, match="allowlist_ttl_s"):
        Verifier("http://emu", allowlist_ttl_s=ttl)


async def test_allowlist_result_cannot_rewrite_the_cache():
    v = _verifier(ACTIVE)
    entries = await v.allowlist()
    entries[0]["status"] = "revoked"
    entries.append({"kind": "image", "measurement": "00" * 32, "status": "active", "mock": True})
    await v.verify_record(record(evidence()))                      # still verifies
    with pytest.raises(VerificationError, match="allowlist"):       # still not listed
        await v.verify_record(record(evidence(measurement="00" * 32)))
    await v.aclose()


# --- hardening: hostile record shapes ---------------------------------------


@pytest.mark.parametrize("rec,match", [
    # malformed box key / payee address must never surface a raw ValueError
    ({"operator": WALLET, "box_key": "zz" * 32}, "box_key"),
    ({"operator": WALLET, "box_key": "ab" * 16}, "box_key"),
    ({"operator": WALLET, "box_key": None}, "box_key"),
    ({"operator": WALLET, "box_key": 12345}, "box_key"),
    ({"operator": WALLET, "box_key": " " + "ab" * 32}, "box_key"),
    ({"operator": "0xnothex", "box_key": BOX}, "operator"),
    ({"operator": "0x1234", "box_key": BOX}, "operator"),
    ({"operator": None, "box_key": BOX}, "operator"),
    ({"operator": ["a"], "box_key": BOX}, "operator"),
])
async def test_malformed_record_fields_refuse(rec, match):
    with pytest.raises(VerificationError, match=match):
        await _verifier(ACTIVE).verify_record({**rec, "evidence": evidence()})


async def test_non_dict_record_refuses():
    with pytest.raises(VerificationError):
        await _verifier(ACTIVE).verify_record("not-a-record")  # type: ignore[arg-type]


@pytest.mark.parametrize("ev_type", [["mock-cvm-v1"], {"a": 1}, 7, None])
async def test_non_str_evidence_type_refuses(ev_type):
    # an unhashable type tag must not blow up the frozenset membership test
    with pytest.raises(VerificationError, match="evidence type"):
        await _verifier(ACTIVE).verify_record(record(evidence(type_=ev_type)))


@pytest.mark.parametrize("measurement", [None, "", 7, ["aa"], {"m": 1}])
async def test_missing_or_non_str_measurement_refuses(measurement):
    # a missing measurement must not match an allowlist entry that also lacks one
    entries = ACTIVE + [{"kind": "image", "status": "active", "mock": True}, {"kind": "image", "measurement": "", "status": "active", "mock": True}]
    with pytest.raises(VerificationError, match="measurement"):
        await _verifier(entries).verify_record(record(evidence(measurement=measurement)))


async def test_measurement_match_is_case_insensitive():
    entries = [{**ACTIVE[0], "measurement": MEASUREMENT.upper()}]
    await _verifier(entries).verify_record(record(evidence()))  # no raise


@pytest.mark.parametrize("entry_m", [MEASUREMENT, "0x" + MEASUREMENT, "0X" + MEASUREMENT.upper()])
@pytest.mark.parametrize("ev_m", [MEASUREMENT, "0x" + MEASUREMENT, MEASUREMENT.upper()])
async def test_measurement_0x_prefix_is_the_same_digest(entry_m, ev_m):
    entries = [{**ACTIVE[0], "measurement": entry_m}]
    await _verifier(entries).verify_record(record(evidence(measurement=ev_m)))  # no raise


@pytest.mark.parametrize("entry_m", [MEASUREMENT, "0x" + MEASUREMENT])
@pytest.mark.parametrize("ev_m", [MEASUREMENT, "0x" + MEASUREMENT])
async def test_revocation_applies_across_0x_spellings(entry_m, ev_m):
    # an operator revoking with the '0x' spelling must not leave the bare-hex
    # measurement live (or vice versa)
    entries = [{**ACTIVE[0], "measurement": entry_m, "status": "revoked"}]
    with pytest.raises(VerificationError, match="revoked"):
        await _verifier(entries).verify_record(record(evidence(measurement=ev_m)))


@pytest.mark.parametrize("ev_m", ["0x", "not-hex", MEASUREMENT + "zz", "0x0x" + MEASUREMENT])
async def test_non_hex_measurement_refuses(ev_m):
    entries = ACTIVE + [{"kind": "image", "measurement": "0x", "status": "active", "mock": True},
                        {"kind": "image", "measurement": "not-hex", "status": "active", "mock": True}]
    with pytest.raises(VerificationError, match="measurement"):
        await _verifier(entries).verify_record(record(evidence(measurement=ev_m)))


@pytest.mark.parametrize("tcb", [
    {"svn": "3"}, {"svn": None}, {"svn": float("nan")}, {"svn": float("inf")},
    {"svn": [3]}, {}, None, "svn=3", 7, True,
])
async def test_malformed_tcb_refuses(tcb):
    ev = {**evidence(), "tcb": tcb}
    with pytest.raises(VerificationError, match="TCB"):
        await _verifier(ACTIVE).verify_record(record(ev))


async def test_bool_svn_is_not_a_tcb_version():
    ev = {**evidence(), "tcb": {"svn": True}}
    with pytest.raises(VerificationError, match="TCB"):
        await _verifier(ACTIVE).verify_record(record(ev))


async def test_higher_tcb_floor_refuses_lower_svn():
    v = Verifier("http://emu", mode="mock", min_tcb_svn=5,
                 transport=httpx.MockTransport(
                     lambda r: httpx.Response(200, json={"entries": ACTIVE})))
    with pytest.raises(VerificationError, match="TCB"):
        await v.verify_record(record(evidence(svn=4)))
    await v.verify_record(record(evidence(svn=5)))  # no raise
    await v.aclose()


@pytest.mark.parametrize("debug", [True, 1, "false", [0], {"on": False}])
async def test_any_truthy_debug_marker_refuses(debug):
    with pytest.raises(VerificationError, match="debug"):
        await _verifier(ACTIVE).verify_record(record(evidence(debug=debug)))


@pytest.mark.parametrize("ev_patch", [
    {},                     # 'debug' key removed entirely
    {"debug": None},
    {"debug": 0},           # falsy, but not a stated boolean
    {"debug": "false"},
])
async def test_unstated_debug_status_refuses(ev_patch):
    # A check that cannot be evaluated is a failed check: evidence that never
    # states its debug status has not shown the payload is production-locked.
    ev = {k: v for k, v in evidence().items() if k != "debug"}
    ev.update(ev_patch)
    with pytest.raises(VerificationError, match="debug"):
        await _verifier(ACTIVE).verify_record(record(ev))


@pytest.mark.parametrize("rd", [None, 7, ["ab" * 32], {"rd": 1}, ""])
async def test_report_data_comparison_rejects_non_str(rd):
    ev = {**evidence(), "report_data": rd}
    with pytest.raises(VerificationError, match="bind"):
        await _verifier(ACTIVE).verify_record(record(ev))


async def test_report_data_comparison_accepts_uppercase_hex():
    ev = {**evidence(), "report_data": report_data(BOX, WALLET).upper()}
    await _verifier(ACTIVE).verify_record(record(ev))  # no raise


async def test_report_data_is_bound_to_this_record_not_another():
    # evidence lifted from a different provider's record must not verify here
    other = "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc"
    ev = {**evidence(), "report_data": report_data(BOX, other)}
    with pytest.raises(VerificationError, match="bind"):
        await _verifier(ACTIVE).verify_record(record(ev))
    ev2 = {**evidence(), "report_data": report_data("cd" * 32, WALLET)}
    with pytest.raises(VerificationError, match="bind"):
        await _verifier(ACTIVE).verify_record(record(ev2))


# --- hardening: hostile allowlist -------------------------------------------


@pytest.mark.parametrize("body", [
    [],                                     # envelope is not an object
    "entries",
    {"entries": "not-a-list"},
    {"entries": {"a": 1}},
    {"entries": [ACTIVE[0], "bare-string"]},
    {"entries": [ACTIVE[0], ["kind", "image"]]},
    {"entries": [None]},
])
async def test_malformed_allowlist_envelope_refuses(body):
    v = Verifier("http://emu", mode="mock",
                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(VerificationError, match="allowlist"):
        await v.verify_record(record(evidence()))
    await v.aclose()


async def test_missing_entries_key_is_an_empty_allowlist():
    v = Verifier("http://emu", mode="mock",
                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert await v.allowlist() == []
    with pytest.raises(VerificationError, match="allowlist"):
        await v.verify_record(record(evidence()))
    await v.aclose()


async def test_revocation_wins_over_a_duplicate_active_entry():
    # a hostile/sloppy allowlist that re-lists a revoked measurement as active
    # must not resurrect it: any revoked match refuses.
    entries = [
        {**ACTIVE[0], "status": "revoked"},
        {**ACTIVE[0], "status": "active"},
    ]
    with pytest.raises(VerificationError, match="revoked"):
        await _verifier(entries).verify_record(record(evidence()))
    # ...in either order
    with pytest.raises(VerificationError, match="revoked"):
        await _verifier(list(reversed(entries))).verify_record(record(evidence()))


async def test_unknown_entry_status_is_not_active():
    entries = [{**ACTIVE[0], "status": "pending"}]
    with pytest.raises(VerificationError):
        await _verifier(entries).verify_record(record(evidence()))


async def test_mock_entry_refused_outside_mock_mode_even_when_shadowed():
    # The entry-level mock check guards the future strict tier: today no non-mock
    # evidence tag has a validator, so the type check refuses first and this path
    # is exercised directly. A non-mock duplicate must not shadow a mock entry.
    entries = [ACTIVE[0], {"kind": "image", "measurement": MEASUREMENT,
                           "release": "prod", "status": "active"}]
    v = _verifier(entries, mode="structural")
    with pytest.raises(VerificationError, match="mock allowlist entry"):
        await v._match_image_entry(MEASUREMENT)
    await v.aclose()


async def test_policy_entries_are_not_treated_as_images():
    entries = [{"kind": "policy", "policy_id": "p", "status": "active",
                "measurement": MEASUREMENT}]
    with pytest.raises(VerificationError, match="allowlist"):
        await _verifier(entries).verify_record(record(evidence()))


async def test_allowlist_http_error_propagates_loudly():
    v = Verifier("http://emu", mode="mock",
                 transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    with pytest.raises(httpx.HTTPStatusError):
        await v.allowlist()
    await v.aclose()


async def test_non_json_allowlist_refuses():
    v = Verifier("http://emu", mode="mock",
                 transport=httpx.MockTransport(
                     lambda r: httpx.Response(200, content=b"<html>nope</html>")))
    with pytest.raises(VerificationError, match="allowlist"):
        await v.allowlist()
    await v.aclose()


# --- hardening: verify_candidates -------------------------------------------


async def test_candidates_with_hostile_shapes_are_dropped():
    records = {1: record(evidence(), 1)}
    v = _verifier(ACTIVE, records)
    kept = await v.verify_candidates([
        "not-a-dict",                                   # type: ignore[list-item]
        {"box_key": BOX},                               # no provider id
        {"provider": None, "box_key": BOX},
        {"provider": "1/../allowlist", "box_key": BOX},  # path injection attempt
        {"provider": "../../evm/allowlist", "box_key": BOX},
        {"provider": "http://evil.example/x", "box_key": BOX},
        {"provider": True, "box_key": BOX},             # bool is not a provider id
        {"provider": 1, "box_key": None},               # no seal target
        {"provider": 1, "box_key": 1234},
        {"provider": 1, "box_key": BOX},                # a keeper, under the old key
        {"provider_id": 1, "box_key": BOX},             # and under the node's key
    ])  # type: ignore[arg-type]
    assert kept == [{"provider": 1, "box_key": BOX}, {"provider_id": 1, "box_key": BOX}]


async def test_candidates_are_kept_in_challenge_order_so_the_first_survivor_is_the_seal_target():
    """The node ranks; the client only filters. Provider 2 has no record here,
    so it drops and 1 is first — and when both verify, 2 stays ahead of 1."""
    v = _verifier(ACTIVE, {1: record(evidence(), 1)})
    kept = await v.verify_candidates([{"provider_id": 2, "box_key": BOX},
                                      {"provider_id": 1, "box_key": BOX}])
    assert [c["provider_id"] for c in kept] == [1]

    both = _verifier(ACTIVE, {1: record(evidence(), 1), 2: record(evidence(), 2)})
    kept = await both.verify_candidates([{"provider_id": 2, "box_key": BOX},
                                         {"provider_id": 1, "box_key": BOX}])
    assert [c["provider_id"] for c in kept] == [2, 1]


async def test_candidate_dropped_when_the_record_answers_for_another_provider():
    records = {1: {**record(evidence()), "provider": 9}}     # misrouted read
    v = _verifier(ACTIVE, records)
    assert await v.verify_candidates([{"provider": 1, "box_key": BOX}]) == []


async def test_candidate_kept_when_the_record_omits_the_provider_echo():
    # the echo is transport metadata, not an attester claim; the box-key pin is
    # what makes the seal target safe
    rec = {k: v for k, v in record(evidence()).items() if k != "provider"}
    v = _verifier(ACTIVE, {1: rec})
    kept = await v.verify_candidates([{"provider": 1, "box_key": BOX}])
    assert [c["provider"] for c in kept] == [1]


async def test_candidate_box_key_match_is_case_insensitive():
    records = {1: record(evidence(), 1)}
    v = _verifier(ACTIVE, records)
    kept = await v.verify_candidates([{"provider": 1, "box_key": BOX.upper()}])
    assert [c["provider"] for c in kept] == [1]


async def test_candidate_with_non_dict_record_body_is_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": ACTIVE})
        return httpx.Response(200, json=["not", "a", "record"])

    v = Verifier("http://emu", mode="mock", transport=httpx.MockTransport(handler))
    assert await v.verify_candidates([{"provider": 1, "box_key": BOX}]) == []
    await v.aclose()


async def test_candidate_with_unparseable_record_body_is_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": ACTIVE})
        return httpx.Response(200, content=b"<html>nope</html>")

    v = Verifier("http://emu", mode="mock", transport=httpx.MockTransport(handler))
    assert await v.verify_candidates([{"provider": 1, "box_key": BOX}]) == []
    await v.aclose()


async def test_verify_candidates_preserves_input_order_and_empty_input():
    records = {i: record(evidence(), i) for i in (1, 2, 3)}
    v = _verifier(ACTIVE, records)
    assert await v.verify_candidates([]) == []
    kept = await v.verify_candidates([
        {"provider": 3, "box_key": BOX}, {"provider": 1, "box_key": BOX},
        {"provider": 2, "box_key": BOX},
    ])
    assert [c["provider"] for c in kept] == [3, 1, 2]


async def test_candidates_surface_a_malformed_allowlist_instead_of_emptying():
    # An unreadable allowlist must not look like "every provider failed".
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": "nope"})
        return httpx.Response(200, json=record(evidence()))

    v = Verifier("http://emu", mode="mock", transport=httpx.MockTransport(handler))
    with pytest.raises(VerificationError, match="allowlist"):
        await v.verify_candidates([{"provider": 1, "box_key": BOX}])
    await v.aclose()


async def test_candidate_with_no_record_is_dropped_and_the_others_stand():
    # 404 is an answer: that candidate has nothing to attest, the read worked.
    v = _verifier(ACTIVE, {2: record(evidence(), 2)})
    kept = await v.verify_candidates([
        {"provider": 1, "box_key": BOX},   # no record — dropped
        {"provider": 2, "box_key": BOX},
    ])
    assert [c["provider"] for c in kept] == [2]


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_candidates_surface_an_unreadable_record_instead_of_dropping_it(status):
    # An auth or infrastructure fault must not masquerade as "this provider
    # failed verification" — that would silently narrow the field.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(200, json={"entries": ACTIVE})
        return httpx.Response(status, json={})

    v = Verifier("http://emu", mode="mock", transport=httpx.MockTransport(handler))
    with pytest.raises(VerificationError, match="unreadable"):
        await v.verify_candidates([{"provider": 1, "box_key": BOX}])
    await v.aclose()


async def test_verify_candidates_drops_everything_in_structural_mode():
    # mock chain state + mock evidence: a production-mode client keeps nothing.
    records = {1: record(evidence(), 1)}
    v = _verifier(ACTIVE, records, mode="structural")
    assert await v.verify_candidates([{"provider": 1, "box_key": BOX}]) == []


# --- the coordinator escrow key: its own path, its own binding (Q11a) ---------

from vorq.verify import (  # noqa: E402
    ESCROW_SERVICE_ID,
    KEY_FRESHNESS_S,
    MOCK_COORDINATOR_EVIDENCE_TYPE,
    MOCK_EVIDENCE_TYPES,
    MOCK_PROVIDER_EVIDENCE_TYPE,
    STATIC_COORDINATOR_EVIDENCE_TYPE,
    escrow_report_data,
)

ESCROW_MEASUREMENT = hashlib.sha256(b"vorq-mock-coordinator-image-v1").hexdigest()
ESCROW_BOX = "1f" * 32
ESCROW_ACTIVE = [{"kind": "image", "measurement": ESCROW_MEASUREMENT,
                  "status": "active", "mock": True}]
NOW = 1_790_000_000.0


def announcement(*, key=ESCROW_BOX, measurement=ESCROW_MEASUREMENT, rd=None,
                 debug=False, svn=1, type_=MOCK_COORDINATOR_EVIDENCE_TYPE,
                 issued_at=int(NOW), **over):
    body = {
        "escrow_public_key": key,
        "issued_at": issued_at,
        "evidence": {"type": type_, "measurement": measurement,
                     "report_data": rd if rd is not None else escrow_report_data(key),
                     "debug": debug, "tcb": {"svn": svn}, "release": 1, "quote": "bW9jaw=="},
    }
    body.update(over)
    return body


def _escrow_verifier(entries=None, mode="mock", now=NOW, min_tcb_svn=1):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evm/allowlist":
            return httpx.Response(
                200, json={"entries": ESCROW_ACTIVE if entries is None else entries,
                           "as_of_block": 128}
            )
        return httpx.Response(404, json={})

    return Verifier("http://node", mode=mode, min_tcb_svn=min_tcb_svn,
                    transport=httpx.MockTransport(handler), wall_clock=lambda: now)


async def test_the_escrow_binding_is_sha256_of_the_key_and_the_service_id():
    """`sha256(escrow_pk32 ‖ utf8("vorq-coordinator-escrow-v1"))`, recomputed here
    from the two inputs rather than from the implementation."""
    expected = hashlib.sha256(
        bytes.fromhex(ESCROW_BOX) + b"vorq-coordinator-escrow-v1"
    ).hexdigest()
    assert escrow_report_data(ESCROW_BOX) == expected


async def test_the_service_id_is_utf8_and_unpadded():
    """Stated as bytes because the node states it as bytes: a Python side that
    padded it to 32 bytes would refuse every honest node."""
    assert ESCROW_SERVICE_ID == b"vorq-coordinator-escrow-v1"
    assert len(ESCROW_SERVICE_ID) == 26


async def test_a_valid_announcement_returns_the_key_it_announces():
    assert await _escrow_verifier().verify_escrow_key(announcement()) == ESCROW_BOX


async def test_the_escrow_binding_is_not_the_record_binding():
    """Q11a, in two halves, because both of them close the same door.

    Adding `mock-coordinator-v1` to `MOCK_EVIDENCE_TYPES` alone would route the
    announcement into `verify_record`. That is wrong twice over: the tag belongs
    to a different trust domain, and the record binding is
    `sha256(box_key ‖ address)` over an `address` `GET /key` does not have and
    never will — the escrow has no payee. It would raise on every honest node,
    which reads exactly like a verification failure and is not one.
    """
    body = announcement()
    # (a) the tag is not a provider's, whatever the shape of the record.
    with pytest.raises(VerificationError, match="unrecognized evidence type"):
        await _escrow_verifier().verify_record(
            {"box_key": body["escrow_public_key"], "operator": WALLET,
             "evidence": body["evidence"]}
        )
    # (b) and the record binding cannot even be computed for an announcement.
    assert "operator" not in body
    with pytest.raises(VerificationError, match="operator"):
        report_data(body["escrow_public_key"], body.get("operator"))
    # The two digests are different functions of the same key, by construction.
    assert escrow_report_data(ESCROW_BOX) != report_data(ESCROW_BOX, WALLET)


@pytest.mark.parametrize("kwargs,match", [
    ({"rd": "00" * 32}, "does not bind"),
    ({"measurement": "00" * 32}, "allowlist"),
    ({"debug": True}, "debug"),
    ({"svn": 0}, "TCB"),
    ({"type_": MOCK_PROVIDER_EVIDENCE_TYPE}, "unrecognized escrow evidence type"),
    ({"type_": "unknown-v9"}, "unrecognized escrow evidence type"),
])
async def test_escrow_refusals(kwargs, match):
    with pytest.raises(VerificationError, match=match):
        await _escrow_verifier().verify_escrow_key(announcement(**kwargs))


@pytest.mark.parametrize("key", [None, "not-a-key", "ab" * 31, 7])
async def test_an_announcement_with_no_usable_key_refuses(key):
    body = announcement()
    body["escrow_public_key"] = key
    with pytest.raises(VerificationError, match="escrow_public_key"):
        await _escrow_verifier().verify_escrow_key(body)


async def test_a_revoked_escrow_measurement_refuses():
    revoked = [{**ESCROW_ACTIVE[0], "status": "revoked"}]
    with pytest.raises(VerificationError, match="revoked"):
        await _escrow_verifier(revoked).verify_escrow_key(announcement())


async def test_mock_escrow_evidence_is_refused_outside_mock_mode():
    with pytest.raises(VerificationError, match="mock"):
        await _escrow_verifier(mode="structural").verify_escrow_key(announcement())


async def test_an_announcement_with_no_evidence_refuses():
    with pytest.raises(VerificationError, match="no attestation evidence"):
        await _escrow_verifier().verify_escrow_key(announcement(evidence=None))


# --- Q23: issued_at, ±600 s --------------------------------------------------


@pytest.mark.parametrize("offset", [0, KEY_FRESHNESS_S, -KEY_FRESHNESS_S])
async def test_an_announcement_inside_the_freshness_bound_is_accepted(offset):
    body = announcement(issued_at=int(NOW + offset))
    assert await _escrow_verifier().verify_escrow_key(body) == ESCROW_BOX


@pytest.mark.parametrize("offset", [KEY_FRESHNESS_S + 1, -(KEY_FRESHNESS_S + 1)])
async def test_an_announcement_outside_the_freshness_bound_refuses(offset):
    """A replayed announcement is one a node made and no longer stands behind."""
    with pytest.raises(VerificationError, match="freshness bound"):
        await _escrow_verifier().verify_escrow_key(announcement(issued_at=int(NOW + offset)))


@pytest.mark.parametrize("issued_at", [None, "1790000000", True, float("inf")])
async def test_an_unusable_issued_at_refuses(issued_at):
    """A check that cannot be evaluated is a failed check."""
    with pytest.raises(VerificationError, match="issued_at"):
        await _escrow_verifier().verify_escrow_key(announcement(issued_at=issued_at))


async def test_freshness_is_checked_before_any_chain_read():
    """A replay costs no chain traffic."""
    reads = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        reads["n"] += 1
        return httpx.Response(200, json={"entries": ESCROW_ACTIVE, "as_of_block": 128})

    v = Verifier("http://node", mode="mock", transport=httpx.MockTransport(handler),
                 wall_clock=lambda: NOW)
    with pytest.raises(VerificationError, match="freshness bound"):
        await v.verify_escrow_key(announcement(issued_at=1))
    assert reads["n"] == 0


# --- the two mock tags are two trust domains ---------------------------------


async def test_the_mock_evidence_types_carry_both_tags():
    assert MOCK_EVIDENCE_TYPES == {"mock-cvm-v1", "mock-coordinator-v1"}


async def test_a_provider_record_carrying_coordinator_evidence_is_refused():
    """A verifier that took either tag for either would accept a provider's
    evidence as proof about the coordinator's escrow key."""
    ev = evidence(type_=MOCK_COORDINATOR_EVIDENCE_TYPE)
    with pytest.raises(VerificationError, match="unrecognized evidence type"):
        await _verifier(ACTIVE).verify_record(record(ev))


# --- Q11b: the node serves `box_key`, and the rename is load-bearing ---------


async def test_a_record_in_the_nodes_own_shape_verifies():
    """`GET /evm/providers/{id}` answers `box_key` (`src/api/routes/providers.ts`).

    Before the rename, `verify_record` bound `box_public_key` and
    `verify_candidates` compared against it — a field the node never sends. The
    binding then raised on every honest record and `verify_candidates` dropped
    **every** candidate, returning an empty list rather than an error. This is
    the assertion that says which spelling is the wire's.
    """
    node_shaped = {
        "provider_id": 7, "provider": 7,
        "operator": WALLET,
        "box_key": BOX, "evidence": evidence(),
        "listed": True, "reputation": 1000, "capacity": 8, "active_jobs": 0,
    }
    await _verifier(ACTIVE).verify_record(node_shaped)   # no raise
    kept = await _verifier(ACTIVE, records={7: node_shaped}).verify_candidates(
        [{"provider": 7, "box_key": BOX}]
    )
    assert kept == [{"provider": 7, "box_key": BOX}]


async def test_a_record_using_the_old_field_name_is_refused_rather_than_ignored():
    """The old spelling is not a synonym: a record that only carries it has no
    key to bind, and that is a refusal."""
    stale = {"provider": 7, "operator": WALLET, "box_public_key": BOX, "evidence": evidence()}
    with pytest.raises(VerificationError, match="box_key"):
        await _verifier(ACTIVE).verify_record(stale)


# --- the allowlist shape Plan 2 actually serves ------------------------------


async def test_the_chain_projected_allowlist_shape_is_read():
    """`{key, status: <int>, entry: {kind, measurement}}` — the row the node
    serves, where `status` is the curation contract's integer (`1` active,
    `2` revoked) and everything descriptive sits inside the entry blob.

    A verifier that read only the flat form would match nothing at all: no
    exception, no diagnostic, just an allowlist that refuses every honest
    provider.
    """
    entries = [{"key": "0x" + "aa" * 32, "status": 1,
                "entry": {"kind": "cvm-image", "measurement": MEASUREMENT, "mock": True}}]
    await _verifier(entries).verify_record(record(evidence()))   # no raise


async def test_a_chain_projected_revocation_is_a_tombstone_and_not_a_deletion():
    entries = [{"key": "0x" + "aa" * 32, "status": 2,
                "entry": {"kind": "cvm-image", "measurement": MEASUREMENT, "mock": True}}]
    with pytest.raises(VerificationError, match="revoked"):
        await _verifier(entries).verify_record(record(evidence()))


async def test_an_unreadable_status_is_neither_active_nor_revoked():
    entries = [{"key": "0x" + "aa" * 32, "status": 99,
                "entry": {"kind": "cvm-image", "measurement": MEASUREMENT, "mock": True}}]
    with pytest.raises(VerificationError, match="not active"):
        await _verifier(entries).verify_record(record(evidence()))


async def test_as_of_block_is_carried_and_ignored():
    """The envelope is `{entries, as_of_block}` and unsigned — the chain is the
    authority, so re-checking a curation signature would buy nothing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entries": ACTIVE, "as_of_block": 4096})

    v = Verifier("http://node", mode="mock", transport=httpx.MockTransport(handler))
    assert await v.allowlist() == ACTIVE


# --- static-coordinator-v1: an operator-keyed escrow, accepted by default -----


def static_announcement(*, key=ESCROW_BOX, rd=None, debug=False,
                        issued_at=int(NOW), **over):
    """A static node's announcement: the binding, and nothing it cannot claim.

    No `measurement`, no `tcb`, no `quote` — there is no image, no platform and
    no report. Building it that way here rather than deleting keys from
    `announcement()` is the point: the fixture is the wire shape.
    """
    body = {
        "escrow_public_key": key,
        "issued_at": issued_at,
        "evidence": {
            "type": STATIC_COORDINATOR_EVIDENCE_TYPE,
            "report_data": rd if rd is not None else escrow_report_data(key),
            "debug": debug,
            "release": 1,
        },
    }
    body.update(over)
    return body


def _refusing_verifier(mode="structural", now=NOW):
    """A verifier whose transport fails any request.

    The static path must read no allowlist: there is no measurement to look up,
    and a client that reached for one would make a chain read to seal every open
    order. A transport that raises is how that is asserted rather than assumed.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the static path must make no request, got {request.url.path}")

    return Verifier("http://node", mode=mode, transport=httpx.MockTransport(handler),
                    wall_clock=lambda: now)


@pytest.mark.parametrize("mode", ["structural", "mock"])
async def test_static_escrow_evidence_is_accepted_in_every_mode(mode):
    """Accepted under the default mode, so a user of a static fleet configures
    nothing. Unlike the mock tag, this one claims nothing beyond the binding
    itself — and that smaller, true claim is exactly what is checked below."""
    v = _refusing_verifier(mode=mode)
    assert await v.verify_escrow_key(static_announcement()) == ESCROW_BOX


async def test_static_escrow_evidence_reads_no_allowlist():
    """No measurement means no image to resolve, and no chain read to seal an
    open order. The transport raises on any request."""
    assert await _refusing_verifier().verify_escrow_key(static_announcement()) == ESCROW_BOX


@pytest.mark.parametrize("kwargs,match", [
    ({"rd": "00" * 32}, "does not bind"),
    ({"debug": True}, "debug"),
    ({"issued_at": int(NOW + KEY_FRESHNESS_S + 1)}, "freshness bound"),
    ({"issued_at": None}, "issued_at"),
])
async def test_static_escrow_refusals(kwargs, match):
    with pytest.raises(VerificationError, match=match):
        await _refusing_verifier().verify_escrow_key(static_announcement(**kwargs))


async def test_static_escrow_evidence_needs_a_boolean_debug_flag():
    """A check that cannot be evaluated is a failed check: evidence that never
    states its debug status has not shown the payload is production-locked."""
    body = static_announcement()
    del body["evidence"]["debug"]
    with pytest.raises(VerificationError, match="debug"):
        await _refusing_verifier().verify_escrow_key(body)


async def test_the_static_tag_does_not_soften_the_mock_guard():
    """The reason for a second tag rather than reusing the mock one: mock
    evidence is computable by anyone and a mock node hands its whole key set to
    any caller, so its refusal outside mock mode must survive this change."""
    with pytest.raises(VerificationError, match="mock"):
        await _escrow_verifier(mode="structural").verify_escrow_key(announcement())


async def test_the_provider_mock_tag_is_still_refused_on_the_escrow_path():
    with pytest.raises(VerificationError, match="unrecognized escrow evidence type"):
        await _escrow_verifier().verify_escrow_key(
            announcement(type_=MOCK_PROVIDER_EVIDENCE_TYPE)
        )
