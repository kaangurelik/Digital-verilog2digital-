"""Stdlib-only fake CLI for native bridge ownership/protocol tests."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time


def emit(kind, value):
    print(json.dumps({"schema_version": 1, "type": kind,
                      "event" if kind == "progress" else "result": value}, ensure_ascii=False), flush=True)


if sys.prefix == sys.base_prefix or Path(sys.executable).parent.name.lower() != "scripts":
    raise RuntimeError("Fixture must run through an isolated .venv")

args = sys.argv[1:]
if args and args[0] == "__sleep":
    time.sleep(60)
    raise SystemExit(0)
if args[:2] == ["job", "cancel"]:
    manifest = Path(args[2])
    (manifest.parent / "cancel.json").write_text("{}", encoding="utf-8")
    emit("result", {"accepted": True})
    raise SystemExit(0)

mode = args[args.index("--fixture") + 1]
if mode == "echo":
    os.write(2, "diagnostic ışık π\n".encode())
    emit("result", {"state": "ready", "argv": args, "prefix": sys.prefix,
                    "isolated": sys.flags.isolated, "pythonhome": os.environ.get("PYTHONHOME"),
                    "pythonpath": os.environ.get("PYTHONPATH"), "root": os.environ.get("V2D_ROOT"),
                    "job_root": os.environ.get("V2D_JOB_ROOT")})
elif mode == "flood":
    for index in range(6000):
        emit("progress", {"stage": "routing", "index": index})
    emit("result", {"state": "ready"})
elif mode in {"late-job", "desktop-final"}:
    time.sleep(.35)
    identifier = "f" * 32
    directory = Path(os.environ["V2D_JOB_ROOT"]) / identifier
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "cancel.json"
    marker.unlink(missing_ok=True)
    manifest = directory / "job.json"
    manifest.write_text("{}", encoding="utf-8")
    emit("progress", {"stage": "inspecting", "state": "running", "sequence": 0,
                      "job_id": identifier, "job_manifest": str(manifest)})
    if mode == "desktop-final":
        emit("result", {"state": "ready", "job_manifest": str(manifest)})
        raise SystemExit(0)
    while not marker.exists():
        time.sleep(.01)
    emit("error", {"state": "cancelled", "job_id": identifier, "job_manifest": str(manifest),
                   "diagnostic": {"code": "CANCELLED", "message": "Cancelled through job API"}})
    raise SystemExit(1)
elif mode == "wrong-job-path":
    emit("progress", {"stage": "routing", "job_id": "f" * 32,
                      "job_manifest": str(Path(os.environ["V2D_JOB_ROOT"]) / "other" / "job.json")})
    time.sleep(60)
elif mode in {"hang-child", "leak-child"}:
    marker = Path(args[args.index("--marker") + 1])
    child = subprocess.Popen([sys.executable, "-I", "-X", "utf8", __file__, "__sleep"])
    marker.write_text(json.dumps({"parent": os.getpid(), "child": child.pid}), encoding="utf-8")
    if mode == "leak-child":
        emit("result", {"state": "ready"})
    else:
        time.sleep(60)
elif mode == "result-hang":
    emit("result", {"state": "ready"})
    time.sleep(60)
elif mode == "duplicate":
    os.write(1, b'{"schema_version":1,"type":"result","result":{"state":"ready","state":"ready"}}\n')
elif mode == "invalid-utf8":
    os.write(1, b'{"schema_version":1,"type":"result","result":{"value":"\xff"}}\n')
elif mode == "surrogate":
    os.write(1, b'{"schema_version":1,"type":"result","result":{"value":"\\ud800"}}\n')
elif mode == "nonfinite":
    os.write(1, b'{"schema_version":1,"type":"result","result":{"value":1e9999}}\n')
elif mode == "depth":
    os.write(1, ('{"schema_version":1,"type":"result","result":{"value":' + '[' * 70 + '0' + ']' * 70 + '}}\n').encode())
elif mode == "extra-field":
    os.write(1, b'{"schema_version":1,"type":"result","result":{},"extra":true}\n')
elif mode == "unterminated":
    os.write(1, b'{"schema_version":1,"type":"result","result":{}}')
elif mode == "after-result":
    emit("result", {"state": "ready"})
    emit("progress", {"stage": "routing"})
elif mode == "oversize":
    os.write(1, b'x' * 32768)
    time.sleep(60)
elif mode == "stderr-flood":
    os.write(2, b'x' * 32768)
    time.sleep(60)
elif mode == "log-bytes":
    os.write(2, b'\xff' * 200)
    emit("result", {"state": "ready"})
elif mode == "exit-mismatch":
    emit("result", {"state": "ready"})
    raise SystemExit(1)
elif mode == "empty":
    pass
else:
    raise RuntimeError("Unknown test fixture")
