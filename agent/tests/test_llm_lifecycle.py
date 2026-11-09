"""Real owned HTTP-process cancellation without loading model weights in unit runs."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from verilog2digital.llm import runtime
from verilog2digital.llm.resources import select_profile

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owned runtime")

SERVER = r'''
from http.server import BaseHTTPRequestHandler,HTTPServer
import json,sys,time
port=int(sys.argv[1]); delay=float(sys.argv[2])
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  data=json.dumps({'status':'ok'} if self.path=='/health' else {'is_sleeping':False}).encode()
  self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']))
  if self.path=='/apply-template': data={'prompt':'formatted'}
  elif self.path=='/tokenize': data={'tokens':[1,2,3]}
  else:
   time.sleep(delay)
   data={'choices':[{'finish_reason':'stop','message':{'content':'{"decision":"keep"}'}}]}
  data=json.dumps(data).encode()
  self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


def client(monkeypatch, tmp_path, *, delay=0):
    resources = {"host": {"available_commit_bytes": 10 * 1024**3, "available_physical_bytes": 14 * 1024**3}, "gpus": []}
    profile = select_profile(resources=resources)
    monkeypatch.setattr(runtime, "verify_installation", lambda *args, **kwargs: {
        "executable": sys.executable, "model_path": "unused", "model": {"id": "test"}, "runtime": {"commit": "test"}})
    monkeypatch.setattr(runtime, "probe_resources", lambda: resources)
    monkeypatch.delenv("V2D_JOB_TMP", raising=False)
    result = runtime.LocalModelRuntime(profile, root=tmp_path)
    monkeypatch.setattr(result, "_arguments", lambda: [sys.executable, "-u", "-c", SERVER, str(result._port), str(delay)])
    return result


def test_actual_owned_runtime_roundtrip_and_closed_process_accounting(monkeypatch, tmp_path):
    instance = client(monkeypatch, tmp_path)
    try:
        instance.start(timeout=10)
        response = instance.generate_json([{"role": "user", "content": "choose"}],
            {"type": "object", "properties": {"decision": {"const": "keep"}}, "required": ["decision"]})
        assert response["value"] == {"decision": "keep"}
        assert instance.status()["process"]["active_processes"] >= 1
        memory = instance.status()["server_memory"]
        assert memory["pid"] == instance._worker.pid
        assert memory["working_set_bytes"] > 0
        assert memory["peak_working_set_bytes"] >= memory["working_set_bytes"]
        assert memory["private_committed_bytes"] > 0
    finally:
        instance.close()
    assert instance.status()["process"]["active_processes"] == 0
    assert instance.status()["server_memory"]["working_set_bytes"] == 0
    assert instance.status()["server_memory"]["peak_working_set_bytes"] > 0
    assert instance._threads == []
    instance.close()


def test_cancel_interrupts_blocked_owned_native_http_and_drains_threads(monkeypatch, tmp_path):
    instance = client(monkeypatch, tmp_path, delay=60)
    instance.start(timeout=10)
    cancel = threading.Event()
    timer = threading.Timer(.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(runtime.ModelError) as caught:
            instance.generate_json([{"role": "user", "content": "choose"}], {"type": "object"},
                                   timeout=30, cancel_event=cancel)
        assert caught.value.code == "MODEL_CANCELLED"
        assert time.monotonic() - started < 3
        assert instance.status()["process"]["active_processes"] == 0
        assert not instance._threads
    finally:
        timer.cancel()
        instance.close()


def test_lease_prevents_second_runtime_without_terminating_first(monkeypatch, tmp_path):
    first = client(monkeypatch, tmp_path)
    second = client(monkeypatch, tmp_path)
    try:
        first.start(timeout=10)
        with pytest.raises(ValueError) as caught:
            second.start(timeout=10)
        assert caught.value.code == "MODEL_BUSY"
        assert first.status()["process"]["active_processes"] >= 1
        assert first.status()["server"]["is_sleeping"] is False
    finally:
        second.close()
        first.close()
