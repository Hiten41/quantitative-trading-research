"""Offline archive discovery, integrity and interrupted-transfer regressions."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from mmbacktest.data import archive


class Response:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.content


def test_retry_corrupt_response_then_reuse_cache(tmp_path, monkeypatch):
    body = b"test archive bytes"
    expected = hashlib.sha256(body).hexdigest()
    item = {"url": "https://example.invalid/trades.zip", "size": len(body), "checksum": True}
    monkeypatch.setattr(archive, "get", lambda url: SimpleNamespace(text=expected + "  trades.zip"))
    attempts = []

    def fetch(*args, **kwargs):
        attempts.append(1)
        return Response(b"corrupt" if len(attempts) == 1 else body)

    monkeypatch.setattr(archive.requests, "get", fetch)
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)
    for _ in range(2):
        row = archive.download_one(item, tmp_path)
        assert row["sha256"] == expected
    assert len(attempts) == 2
    assert [f.name for f in tmp_path.iterdir()] == ["trades.zip"]
    (tmp_path / "trades.zip").write_bytes(b"x" * len(body))
    archive.download_one(item, tmp_path)
    assert len(attempts) == 3 and (tmp_path / "trades.zip").read_bytes() == body


def test_missing_archive_in_actual_listing_rejected(monkeypatch):
    monkeypatch.setattr(
        archive,
        "get",
        lambda *a, **kw: SimpleNamespace(
            content=b'<ListBucketResult xmlns="urn:s3"></ListBucketResult>'
        ),
    )
    with pytest.raises(ValueError, match="missing or empty"):
        archive.discover("ETHUSDT", "2023-09-02", "2023-09-02")


def test_invalid_checksum_never_enters_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "get", lambda url: SimpleNamespace(text="not-a-sha256"))
    with pytest.raises(ValueError, match="checksum"):
        archive.download_one(
            {"url": "https://example.invalid/t.zip", "checksum": True, "size": 3}, tmp_path
        )
    assert not list(tmp_path.iterdir())
