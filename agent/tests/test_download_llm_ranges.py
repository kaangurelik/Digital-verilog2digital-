"""Resumable range downloads must never trust progress for bytes that no longer exist."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import download_llm_ranges as ranges


class Response:
    def __init__(self, data, first, last):
        self.status = 206
        self.headers = {"Content-Range": f"bytes {first}-{last}/{len(data)}"}
        self.body = io.BytesIO(data[first:last + 1])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()

    def read1(self, size):
        return self.body.read(size)


def pinned(monkeypatch, tmp_path, served):
    data = b"pinned-official-model-bytes"
    spec = {"repository": "Qwen/test", "revision": "abc", "filename": "model.gguf", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "layers": 1}
    target = tmp_path / "models" / "model.gguf"
    monkeypatch.setattr(ranges, "load_lock", lambda *_: {"models": {"test": spec}})
    monkeypatch.setattr(ranges, "model_path", lambda *_: target)
    monkeypatch.setattr(ranges, "require_venv", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["download_llm_ranges.py", "--model", "test", "--workers", "1"])

    def urlopen(request, timeout):
        first, last = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
        # Correct length and Content-Range; only the content can differ from the pinned artifact.
        return Response(served["data"], first, last)
    monkeypatch.setattr(ranges.urllib.request, "urlopen", urlopen)
    return data, target


def test_deleted_published_model_downloads_again_instead_of_trusting_stale_ranges(monkeypatch, tmp_path):
    served = {}
    data, target = pinned(monkeypatch, tmp_path, served)
    served["data"] = data
    ranges.main()
    assert target.read_bytes() == data
    target.unlink()
    ranges.main()
    assert target.read_bytes() == data


def test_failed_whole_file_hash_resets_ranges_so_the_next_run_can_recover(monkeypatch, tmp_path):
    served = {}
    data, target = pinned(monkeypatch, tmp_path, served)
    served["data"] = bytes(len(data))
    with pytest.raises(ValueError, match="next run downloads every range again"):
        ranges.main()
    assert not target.exists()
    assert list(target.parent.glob(".range-*")), "Failed staging bytes are retained for inspection"
    progress = json.loads(target.with_suffix(".ranges.json").read_text(encoding="utf-8"))
    assert progress["complete"] == {}
    served["data"] = data
    ranges.main()
    assert target.read_bytes() == data
