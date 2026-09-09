"""Exercise analysis callbacks while native procedure storage reallocates.

The stress module has 32 sibling always blocks, nested calls and generate blocks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

# Isolate native crashes and suppress crash dialogs in the child process.
CHILD = r'''
import os
if os.name == "nt":
    import ctypes
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)

import gc
import json
from pathlib import Path
import sys

from verilog2digital.frontend.errors import FrontendError
from verilog2digital.frontend.inspection import inspect_sources

fixtures, work = map(Path, sys.argv[1:])
valid = work / "complete_function.v"
valid.write_text(
    "module complete_function(input a,b,output reg y); "
    "function f(input x); f=x&b; endfunction "
    "always @(a or b) y=f(a); endmodule", encoding="utf-8")
incomplete = work / "incomplete.v"
incomplete.write_text(
    "module incomplete(input a,b,output reg y); "
    "always @(a) y=a&b; endmodule", encoding="utf-8")
implicit_global = work / "implicit_global.v"
implicit_global.write_text(
    "module implicit_global(input a,b,output reg y); "
    "function f(input x); f=x&b; endfunction "
    "always @* y=f(a); endmodule", encoding="utf-8")

accepted = rejected = 0
for iteration in range(100):
    for source in (fixtures / "lifetime_stress.v", fixtures / "bus_alias.v",
                   fixtures / "always_case.v", valid):
        result = inspect_sources([source], top=source.stem)
        assert result["top"] == source.stem
        assert json.loads(json.dumps(result)) == result
        accepted += 1
        del result
    for source in (incomplete, implicit_global):
        try:
            inspect_sources([source], top=source.stem)
        except FrontendError as error:
            assert error.code == "INCOMPLETE_SENSITIVITY", (error.code, error.message)
            assert Path(error.path) == source and error.line >= 1
            rejected += 1
        else:
            raise AssertionError("Missing expected sensitivity rejection: " + source.name)
    # Exception cleanup must leave the next native compilation usable.
    assert inspect_sources([fixtures / "generated.v"], top="generated")["top"] == "generated"
    accepted += 1
    garbage = [bytearray(1024) for _ in range(256)]
    del garbage
    gc.collect()
    if iteration % 20 == 19:
        print("completed rounds", iteration + 1, flush=True)
print(json.dumps({"rounds": 100, "accepted": accepted, "rejected": rejected}), flush=True)
'''


def test_native_inspection_lifetimes_survive_reallocation_and_repeated_cleanup(tmp_path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "agent/src")
    environment["PYTHONFAULTHANDLER"] = "1"
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", CHILD,
         str(ROOT / "tests/fixtures/verilog/frontend"), str(tmp_path)],
        cwd=ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45,
    )
    assert completed.returncode == 0, (
        f"Native inspection child exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "rounds": 100, "accepted": 500, "rejected": 200,
    }
