"""Shared test helpers: build a Client wired to a routed MockTransport."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

import httpx
import pytest

from vorq import Client


def _load_env_test() -> None:
    """Load throwaway key material from .env.test into the environment.

    Tests read wallet/cipher keys via env indirection; missing keys are left to
    the individual test to skip on, so a fresh clone without .env.test still
    collects cleanly.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env.test"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_test()


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> Client:
    transport = httpx.MockTransport(handler)
    return Client.from_session_token("vorq_test", transport=transport, **kwargs)


def json_response(status: int, body, *, request_id="req_1", retryable=None) -> httpx.Response:
    headers = {"x-request-id": request_id}
    if retryable is not None:
        headers["x-vorq-retryable"] = "true" if retryable else "false"
    return httpx.Response(status, json=body, headers=headers)


def req_json(request: httpx.Request) -> dict:
    return json.loads(request.content)


def multipart_parts(request: httpx.Request) -> list[tuple[str, str | None, bytes]]:
    """A ``multipart/form-data`` body as ``(name, filename, bytes)`` in wire order.

    Order is kept because it is the contract: the node reads the fields ahead of
    the file part and refuses a form whose file arrives first.
    """
    content_type = request.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data"), content_type
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = []
    for chunk in request.content.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break  # the closing delimiter
        head, body = chunk.split(b"\r\n\r\n", 1)
        disposition = next(
            line for line in head.decode().split("\r\n") if line.lower().startswith("content-disposition")
        )
        params = dict(
            p.strip().split("=", 1) for p in disposition.split(";")[1:]
        )
        filename = params.get("filename")
        parts.append((
            params["name"].strip('"'),
            filename.strip('"') if filename is not None else None,
            body.removesuffix(b"\r\n"),
        ))
    return parts


def req_body(request: httpx.Request) -> dict:
    """The request body as one dict, whichever way it was sent.

    A JSON body parses as itself — every job submission, complete or
    terms-only, and every batch line: the container rides inline as base64 or
    is named by ``container_cid``, never as a part of this body. The
    multipart branch now serves only ``POST /v1/files``, whose fields become
    strings and whose one ``file`` part becomes raw ``bytes`` under its field
    name.
    """
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        return {
            name: body if filename is not None else body.decode()
            for name, filename, body in multipart_parts(request)
        }
    return json.loads(request.content)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff / poll sleeps instant."""
    import asyncio

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture
def wait_clock(monkeypatch):
    """A stopped clock that only a poll sleep advances; returns the sleeps taken."""
    import asyncio

    from vorq import _sla

    clock = {"now": 0.0}
    sleeps: list[float] = []

    async def _advance(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(_sla, "now", lambda: clock["now"])
    monkeypatch.setattr(asyncio, "sleep", _advance)
    return sleeps


def json_keys(obj) -> set[str]:
    """Every key name appearing anywhere in a nested JSON structure.

    Asserting a field's absence has to be done on keys, never on a substring of
    the serialised body: a container is base64, and base64 of random bytes
    contains ``dek`` roughly one body in a few hundred. That is a test that
    passes almost always and fails for a reason no one can reproduce.
    """
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(key)
            found |= json_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            found |= json_keys(item)
    return found


def fake_cid(raw: bytes) -> str:
    """A content-derived opaque locator for tests. Real names are minted by the
    filestore that pins the bytes — nothing in the SDK computes or parses one —
    so tests need only a stable per-content string, not any CID encoding."""
    return "cid-test-" + hashlib.sha256(raw).hexdigest()
