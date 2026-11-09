"""Test a copied release from an unrelated working directory.

Sanitize the environment so the package resolves its own dependencies. Confine
mutations to copied packages and fixtures; installed tools remain untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_JSON = 16 * 1024**2
MAX_LOG = 1024**2
CORPUS = Path("examples/fixtures/release/index.json")
PRODUCTION_PACKAGES = {"attrs", "jsonschema", "jsonschema-specifications", "pyslang", "referencing", "rpds-py"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Repeated JSON field: " + key[:100])
        result[key] = value
    return result


def strict_json(data):
    if isinstance(data, str):
        data = data.encode("utf-8", errors="strict")
    require(len(data) <= MAX_JSON, "JSON exceeded the acceptance byte limit")
    value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                       parse_constant=lambda token: (_ for _ in ()).throw(ValueError("Nonfinite JSON: " + token)))
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        require(depth <= 64, "JSON exceeded the acceptance nesting limit")
        if isinstance(item, float):
            require(math.isfinite(item), "JSON contained a nonfinite number")
        if isinstance(item, (str,)):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            for key, child in item.items():
                key.encode("utf-8", errors="strict")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def read_json(path):
    with Path(path).open("rb") as stream:
        return strict_json(stream.read(MAX_JSON + 1))


def write_json(path, value):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(root, name, *, exists=True):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name and "\0" not in name,
            "Expected a relative POSIX artifact path")
    relative = PurePosixPath(name)
    require(relative.parts and not relative.is_absolute() and relative.as_posix() == name and not {".", ".."}.intersection(relative.parts),
            "Artifact path escaped its root")
    for part in relative.parts:
        require(not part.endswith((".", " ")) and not any(ord(char) < 32 or char in '<>"|?*' for char in part)
                and not (part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                         or part.split(".")[0].upper() in {prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"}),
                "Artifact path contains a Windows alias")
    root = Path(root).resolve(strict=True)
    result = root.joinpath(*relative.parts)
    for item in (result, *result.parents):
        if item == root:
            break
        require(not item.is_symlink() and not getattr(item, "is_junction", lambda: False)(), "Artifact path traversed a link")
    require(result.resolve(strict=exists).is_relative_to(root), "Artifact path escaped its root")
    return result


def inside(path, root):
    return Path(path).resolve(strict=True).is_relative_to(Path(root).resolve(strict=True))


def clean_environment(temporary):
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
               "PROGRAMDATA", "USERNAME", "USERDOMAIN", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
               "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS", "PATHEXT",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
               "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    windows = Path(environment.get("SYSTEMROOT", environment.get("SystemRoot", r"C:\Windows")))
    environment["PATH"] = str(windows / "System32") + os.pathsep + str(windows)
    temporary = Path(temporary).resolve(strict=True)
    require(str(temporary).isascii(), "Acceptance temporary root must be ASCII")
    environment.update(TEMP=str(temporary), TMP=str(temporary), PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PIP_NO_INDEX="1")
    return environment


def audit_isolation(doctor, package):
    require(doctor.get("state") == "ready", "Packaged doctor did not report ready")
    bootstrap = doctor["bootstrap"]
    require(Path(bootstrap["root"]).resolve() == Path(package).resolve(), "Launcher selected another installation")
    isolation = bootstrap["isolation"]
    require(Path(isolation["prefix"]).resolve() == (Path(package) / ".venv").resolve(), "Packaged .venv prefix escaped installation")
    require(inside(isolation["executable"], Path(package) / ".venv"), "Interpreter executable is not the packaged .venv")
    require(inside(isolation["base_prefix"], Path(package) / ".tools/python-runtime"), "Python base came from outside the package")
    require(inside(isolation["stdlib"], isolation["base_prefix"]), "Python standard library came from outside the private base")
    require(isolation["packages"] and isolation["native_modules"] and isolation["loaded_dlls"], "Isolation probe omitted package/native DLL origins")
    require(set(isolation["packages"]) == PRODUCTION_PACKAGES, "Isolation probe omitted or added a production distribution")
    for name, entry in isolation["packages"].items():
        require(inside(entry["path"], Path(package) / ".venv"), "External Python package: " + name)
    for name, path in isolation["native_modules"].items():
        require(inside(path, package), "External native Python module: " + name)
    private = []
    for name, path in isolation["loaded_dlls"].items():
        require(inside(path, package), "External audited DLL loaded by packaged interpreter: " + name)
        private.append(name)
    require(any("python314" in name.lower() for name in private), "Probe did not bind the private Python DLL")
    native = Path(bootstrap["native_tools_root"]).resolve(strict=True)
    for name, tool in doctor.get("tools", {}).items():
        require(tool.get("available") is True, "Packaged tool unavailable: " + name)
        if "path" in tool:
            require(inside(tool["path"], package) or inside(tool["path"], native), "Tool escaped verified package/native root: " + name)
    require(str(Path(bootstrap["job_root"])).isascii() and str(native).isascii(), "Native job/tools root is not ASCII")
    fingerprint = doctor.get("fingerprint", {}).get("sha256")
    require(isinstance(fingerprint, str) and len(fingerprint) == 64, "Doctor omitted the runtime fingerprint")
    return {"release": bootstrap["release"], "runtime_fingerprint": fingerprint, "isolation": isolation,
            "job_root": bootstrap["job_root"], "native_tools_root": str(native), "tools": doctor["tools"]}


def artifact_audit(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = read_json(directory / "manifest.json")
    require(manifest.get("state") == "ready", "Published manifest is not ready")
    hashes = {}
    for label, artifact in manifest["artifacts"].items():
        path = contained(directory, artifact["path"])
        actual = digest(path)
        require(actual == artifact["sha256"], "Published artifact hash mismatch: " + label)
        hashes[label] = {"path": artifact["path"], "sha256": actual, "size": path.stat().st_size}
    hashes["manifest"] = {"path": "manifest.json", "sha256": digest(directory / "manifest.json")}
    return manifest, hashes


def audit_conversion(result, case):
    require(result.get("state") == "ready", "Conversion did not select a ready result")
    manifest_path = Path(result["manifest"])
    manifest, hashes = artifact_audit(manifest_path.parent)
    validation = read_json(contained(manifest_path.parent, manifest["artifacts"]["validation"]["path"]))
    require(validation.get("valid") is True, "Conversion validation failed")
    for name in ("components", "topology", "bits", "model", "geometry", "behavior"):
        require(validation.get(name, {}).get("valid") is True, "Missing or failed independent check: " + name)
    behavior = validation["behavior"]
    if "vectors" in case:
        require(behavior.get("coverage") == "exhaustive_binary" and behavior.get("input_vectors") == case["vectors"],
                "Binary coverage differs from the fixed release corpus")
    if "minimum_vectors" in case:
        require(behavior.get("input_vectors", 0) >= case["minimum_vectors"], "Seeded/directed release coverage is incomplete")
    if "cycles" in case:
        require(behavior.get("checked_cycles") == case["cycles"] and behavior.get("input_vectors") == 3 * case["cycles"]
                and behavior.get("assumptions", {}).get("sample_count") == 3 * case["cycles"]
                and behavior.get("phase_order") == ["before_rise", "after_rise", "after_fall"],
                "Sequential cycle/phase coverage differs from the fixed trace")
        require(behavior.get("initialization", {}).get("register_count") == case["registers"], "Not every retained register was initialized")
    if "minimum_modules" in case:
        require(validation.get("hierarchy", {}).get("module_count", 0) >= case["minimum_modules"], "Hierarchy was flattened or lost a specialization")
        require(validation.get("modules") and all(item.get("valid") is True for item in validation["modules"].values()), "Local hierarchy canvas failed")
    record = read_json(result["job_manifest"])
    require(record["state"] == "ready" and record["resources"]["metrics"]["active_processes"] == 0, "Published job retained an owned process")
    require(result.get("cache", {}).get("hit") is not True, "Release corpus reused a cached candidate")
    return {"manifest": str(manifest_path), "artifact_hashes": hashes, "checks": {name: validation[name] for name in
            ("components", "topology", "bits", "model", "geometry")}, "behavior": behavior, "layout": manifest["layout"],
            "job_manifest": result["job_manifest"], "job_metrics": record["resources"]["metrics"],
            "job_seconds": record.get("duration_seconds"), "runtime_fingerprint": record["runtime_fingerprint"]["sha256"]}


def copy_files(source, destination, files):
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    require(not destination.exists(), "Copy destination already exists")
    require(destination.parent.resolve(strict=True).is_relative_to(ROOT.resolve()), "Release copies must remain inside the workspace")
    destination.mkdir()
    for name, expected in files.items():
        origin = contained(source, name)
        target = contained(destination, name, exists=False)
        info = origin.stat(follow_symlinks=False)
        require(stat.S_ISREG(info.st_mode), "Release copy input is not a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        calculated = hashlib.sha256()
        with origin.open("rb") as reader, target.open("xb") as writer:
            while block := reader.read(1024**2):
                calculated.update(block); writer.write(block)
        if expected:
            require(target.stat().st_size == expected["size"] and calculated.hexdigest() == expected["sha256"],
                    "A copied release input changed: " + name)
    return destination


def runtime_files(package, *, models):
    package = Path(package)
    files = dict(read_json(package / "release-manifest.json")["files"])
    files["release-manifest.json"] = {"size": (package / "release-manifest.json").stat().st_size, "sha256": digest(package / "release-manifest.json")}
    if models:
        for relative in (".tools/models", ".tools/llama.cpp", ".tools/downloads"):
            folder = package / relative
            if folder.exists():
                for file in folder.rglob("*"):
                    require(not file.is_symlink() and not getattr(file, "is_junction", lambda: False)(), "Optional runtime contains a link")
                    if file.is_file() and file.name != "runtime.lock":
                        files[file.relative_to(package).as_posix()] = {"size": file.stat().st_size, "sha256": digest(file)}
    return files


class Runner:
    def __init__(self, output, *, timeout=360):
        self.output = Path(output).resolve(strict=True)
        self.cwd = self.output / "unrelated-cwd"; self.cwd.mkdir()
        self.temporary = self.output / "private-temp"; self.temporary.mkdir()
        self.environment = clean_environment(self.temporary)
        self.timeout = timeout
        self.commands = []
        self.contexts = {}
        self.jobs = self.output / "owned-jobs"; self.jobs.mkdir()

    def command(self, package, name, arguments, *, success=True, raw=False, interrupt_when=None, executable=None, timeout=None):
        # Use the harness ownership helper without importing it into the package.
        sys.path.insert(0, str(ROOT / "agent/src"))
        from verilog2digital.jobs.processes import ManagedWorker, ProcessLimits
        deadline_seconds = self.timeout if timeout is None else timeout
        require(isinstance(deadline_seconds, (int, float)) and not isinstance(deadline_seconds, bool)
                and 0 < deadline_seconds <= 3600, "Invalid harness command deadline")
        package = Path(package).resolve(strict=True)
        index = len(self.commands)
        folder = self.output / f"{index:03d}-{name}"; folder.mkdir()
        args = [str(executable or (package / "VerilogToDigital.exe")), *map(str, arguments)]
        started = time.monotonic(); chunks = bytearray(); errors = []; thread = None; worker = None
        record = {"name": name, "argv": args, "cwd": str(self.cwd), "package": str(package), "valid": False,
                  "harness_deadline_seconds": deadline_seconds}
        self.commands.append(record)
        environment = dict(self.environment)
        identity = hashlib.sha256(str(package).encode("utf-8")).hexdigest()[:16]
        job_root = self.jobs / identity; job_root.mkdir(exist_ok=True)
        environment["V2D_JOB_ROOT"] = str(job_root)
        if str(package) in self.contexts:
            environment["V2D_NATIVE_TOOLS_ROOT"] = self.contexts[str(package)]["native_tools_root"]
        if executable is not None:
            environment["V2D_ROOT"] = str(package)
        try:
            worker = ManagedWorker.start(args, cwd=self.cwd, env=environment, stderr_path=folder / "stderr.log",
                limits=ProcessLimits(job_memory_bytes=8 * 1024**3, active_process_limit=128))
            def read():
                try:
                    while block := worker.stdout.read(8192):
                        chunks.extend(block)
                        if len(chunks) > MAX_JSON:
                            raise RuntimeError("Package stdout exceeded 16 MiB")
                except BaseException as error:
                    errors.append(str(error))
            thread = threading.Thread(target=read, daemon=True); thread.start()
            stopped = False
            while worker.poll() is None:
                if errors:
                    raise RuntimeError(errors[0])
                require((folder / "stderr.log").stat().st_size <= MAX_LOG, "Package stderr exceeded 1 MiB")
                require(time.monotonic() - started < deadline_seconds, "Package command exceeded its owned deadline")
                if interrupt_when and interrupt_when():
                    worker.terminate_tree(); stopped = True; break
                time.sleep(.05)
            worker.wait(5)
            drain_deadline = time.monotonic() + 2
            while worker.metrics()["active_processes"] and time.monotonic() < drain_deadline:
                time.sleep(.05)
            before_close = worker.metrics()
            require(before_close["active_processes"] == 0, "Package command leaked an owned process")
            thread.join(2)
            require(not thread.is_alive() and not errors, "Package output reader failed to drain")
            require((folder / "stderr.log").stat().st_size <= MAX_LOG, "Package stderr exceeded 1 MiB")
            record.update(exit_code=worker.poll(), interrupted=stopped, metrics=before_close)
            if not raw and not stopped:
                result = strict_json(chunks)
                require(isinstance(result, dict), "Package command did not return a JSON object")
                record["result"] = result
                if arguments == ["--doctor"] and result.get("state") == "ready":
                    require(Path(result["bootstrap"]["job_root"]).resolve() == job_root.resolve(), "Bootstrap changed the explicitly owned job root")
                    self.contexts[str(package)] = result["bootstrap"]
            else:
                result = None
            require((worker.poll() == 0) == success if not stopped else not success, "Unexpected package command exit code")
            record["valid"] = True
            return result
        finally:
            if worker is not None:
                try:
                    worker.close()
                finally:
                    if thread is not None:
                        thread.join(2)
                    record["closed_metrics"] = worker.metrics()
            (folder / "stdout.log").write_bytes(chunks)
            record["seconds"] = round(time.monotonic() - started, 6)
            record["stdout_sha256"] = hashlib.sha256(chunks).hexdigest()
            record["log_directory"] = str(folder)
            write_json(folder / "command.json", record)

    def cli(self, package, name, arguments, **kwargs):
        return self.command(package, name, ["--cli", *arguments, "--json", "--quiet"], **kwargs)


def compare_native(first, second):
    dependencies = {key for key in first["artifact_hashes"] if key.startswith("dependency_")}
    require(dependencies == {key for key in second["artifact_hashes"] if key.startswith("dependency_")},
            "Native hierarchy dependency inventory changed after relocation")
    for label in ("circuit", "sidecar", "preview", "svg", "preview_metadata", *sorted(dependencies)):
        if label in first["artifact_hashes"]:
            require(first["artifact_hashes"][label]["sha256"] == second["artifact_hashes"][label]["sha256"],
                    "Deterministic artifact changed after installation relocation: " + label)
    require(first["layout"]["metrics"] == second["layout"]["metrics"], "Layout metrics changed after relocation")


def model_acceptance(runner, root, label, projects, output, installation):
    verification = runner.cli(root, label + "-model-verify", ["models", "verify", "--model", "qwen3-4b-q4_k_m", "--backend", "cuda"])
    require(verification.get("verified") is True, "Offline Q4/runtime hashes did not verify")
    project = projects / "release/full_adder/project.json"
    result = runner.cli(root, label + "-model-conversion", ["convert", "--project", project,
        "--output", output / (label + "-model-output"), "--llm", "4b", "--no-cache", "--seed", "0",
        "--refinement-attempts", "1", "--refinement-budget", "30"])
    audit = audit_conversion(result, {"vectors": 8})
    manifest = read_json(result["manifest"])
    refinement = manifest["refinement"]
    runtime = refinement.get("runtime", {})
    require(runtime.get("requests", 0) >= 1 and runtime.get("starts", 0) >= 1, "Optional model did not make a real inference request")
    require(runtime.get("process", {}).get("active_processes") == 0 and runtime["process"].get("closed") is True,
            "Optional model runtime did not close its owned processes")
    responses = [attempt["response"] for canvas in refinement["canvases"] for attempt in canvas["attempts"] if "response" in attempt]
    require(responses, "Model generated no replayable response; record this as incomplete model/replay acceptance")
    replay_file = output / (label + "-responses.json")
    write_json(replay_file, {"schema_version": 1, "responses": responses})
    replay = runner.cli(root, label + "-model-replay", ["convert", "--project", project,
        "--output", output / (label + "-model-replay-output"), "--replay-actions", replay_file,
        "--no-cache", "--seed", "0", "--refinement-attempts", "1", "--refinement-budget", "30"])
    replay_audit = audit_conversion(replay, {"vectors": 8})
    compare_native(audit, replay_audit)
    replay_refinement = read_json(replay["manifest"])["refinement"]
    require(refinement["accepted"] == replay_refinement["accepted"], "Replayed model acceptance changed")
    installation["model"] = {"verification": verification, "runtime": runtime, "accepted": refinement["accepted"],
        "canvases": refinement["canvases"], "conversion": audit, "replay": replay_audit,
        "replay_response_sha256": digest(replay_file), "download_requested": False}


def package_relocation(runner, root, baseline, output, report):
    require(baseline, "No accepted output exists for export/reopen testing")
    selected = baseline.get("port_aliases", next(iter(baseline.values())))
    source = Path(selected["manifest"]).parent
    destination = output / "Exported package ışık π"
    exported = runner.cli(root, "export-bundle", ["desktop", "export", source, "--output", destination])
    require(exported.get("state") == "ready", "Bundle export failed")
    manifest, hashes = artifact_audit(destination)
    require(digest(destination / "manifest.json") == digest(source / "manifest.json"), "Export rewrote the result manifest")
    for label, expected in selected["artifact_hashes"].items():
        require(hashes[label]["sha256"] == expected["sha256"], "Export changed artifact bytes: " + label)
    relocated = output / "Relocated bundle π"
    files = {entry["path"]: {"sha256": entry["sha256"], "size": (destination / entry["path"]).stat().st_size}
             for entry in hashes.values()}
    copy_files(destination, relocated, files)
    validated = runner.cli(root, "relocated-bundle-native", ["validate", relocated, "--artifacts-only"])
    validation = validated.get("validation", validated)
    require(validation.get("valid") is True, "Relocated bundle failed independent native validation")
    reopened = runner.cli(root, "relocated-bundle-preview", ["desktop", "result", relocated])
    require(reopened["summary"]["native_model_valid"] is True and reopened["summary"]["artifact_binding_valid"] is True,
            "Relocated bundle preview was not checked")
    require(reopened["summary"]["behavior_replayed"] is False, "Reopen incorrectly claimed a new HDL simulation")
    report["bundle"] = {"export": exported, "relocated": str(relocated), "artifact_hashes": hashes,
                        "validation": validation, "preview": reopened, "top": manifest["top"]}


def diagnostic_codes(value):
    result = set(); pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("code"), str):
                result.add(item["code"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return result


def negative_acceptance(runner, source_package, root, projects, baseline, output, report):
    case = baseline.get("full_adder", next(iter(baseline.values())))
    original_manifest = Path(case["manifest"])
    user_source = Path(next(iter(read_json(original_manifest)["sources"])))
    original_source_bytes = user_source.read_bytes()
    negatives = []
    try:
        user_source.write_bytes(original_source_bytes + b"\n// release freshness mutation\n")
        changed = runner.cli(root, "source-change-refusal", ["validate", original_manifest], success=False)
        require("SOURCE_CHANGED" in diagnostic_codes(changed), "Changed original source did not produce SOURCE_CHANGED")
        negatives.append({"name": "original_source_change", "codes": sorted(diagnostic_codes(changed)), "valid": True})
    finally:
        user_source.write_bytes(original_source_bytes)
    exported = Path(report["bundle"]["relocated"])
    export_manifest = read_json(exported / "manifest.json")
    preview = contained(exported, export_manifest["artifacts"]["preview"]["path"])
    original_preview = preview.read_bytes()
    try:
        preview.write_bytes(original_preview + b"changed")
        changed = runner.cli(root, "artifact-change-refusal", ["validate", exported, "--artifacts-only"], success=False)
        require(changed.get("validation", changed).get("valid") is False, "Changed preview was accepted")
        negatives.append({"name": "artifact_change", "codes": sorted(diagnostic_codes(changed)), "valid": True})
    finally:
        preview.write_bytes(original_preview)
    broken = copy_files(source_package, output / "Negative runtime copy", runtime_files(source_package, models=False))
    runner.command(broken, "negative-package-baseline", ["--doctor"])
    inventory = read_json(broken / "release-manifest.json")["files"]
    companion = next((name for name in inventory if name.endswith("/lib/libffi-8.dll")), None)
    require(companion is not None, "No pinned HDL companion DLL is available for corruption testing")
    file = contained(broken, companion); original = file.read_bytes()
    for mutation in ("corrupt", "missing"):
        try:
            if mutation == "corrupt":
                changed = bytearray(original); changed[len(changed) // 2] ^= 1; file.write_bytes(changed)
            else:
                file.unlink()
            refusal = runner.command(broken, mutation + "-companion-refusal", ["--doctor"], success=False)
            codes = diagnostic_codes(refusal)
            require(any(code.startswith("RELEASE_") for code in codes), "Bad companion failed without an actionable release-integrity diagnostic")
            negatives.append({"name": mutation + "_companion", "path": companion, "codes": sorted(codes), "valid": True})
        finally:
            file.write_bytes(original)
    require(digest(file) == inventory[companion]["sha256"], "Negative runtime mutation was not restored")
    runner.command(broken, "negative-package-restored", ["--doctor"])
    if "full_adder" in baseline:
        current = original_manifest.parent.parent.parent / "current.json"
        before = digest(current)
        negative_source = output / "unsupported.v"
        for name, text in (("missing-include", '`include "missing-release-header.vh"\nmodule full_adder(input a,output y);assign y=a;endmodule\n'),
                           ("unsupported-delay", "module full_adder(input a,output y);assign #1 y=a;endmodule\n")):
            negative_source.write_text(text, encoding="utf-8")
            refusal = runner.cli(root, name + "-replacement-refusal", ["convert", negative_source, "--top", "full_adder",
                "--output", current.parent, "--replace", "--no-cache", "--llm", "off"], success=False)
            require(digest(current) == before and diagnostic_codes(refusal), "Failed replacement changed the selected valid output")
            negatives.append({"name": name, "codes": sorted(diagnostic_codes(refusal)), "previous_selection_preserved": True, "valid": True})
        job_root = Path(runner.contexts[str(Path(root).resolve())]["job_root"])
        old_jobs = {entry.name for entry in job_root.iterdir() if entry.is_dir()}
        interrupted = {}
        def started_worker():
            for path in job_root.glob("*/job.json"):
                if path.parent.name in old_jobs:
                    continue
                try:
                    record = read_json(path)
                except (OSError, ValueError, RuntimeError):
                    continue
                if record.get("worker_pid") and record.get("state") not in {"ready", "failed", "cancelled", "interrupted"}:
                    interrupted.update(job_id=record["job_id"], stage=record["stage"], worker_pid=record["worker_pid"])
                    return True
            return False
        runner.cli(root, "interrupted-replacement", ["convert", "--project", projects / "release/full_adder/project.json",
            "--output", current.parent, "--replace", "--no-cache", "--llm", "off"], success=False, raw=True, interrupt_when=started_worker)
        require(interrupted and digest(current) == before, "Interrupted replacement did not preserve the prior selection")
        runner.cli(root, "recover-interrupted-job", ["job", "recover", job_root])
        negatives.append({"name": "owned_worker_interruption", **interrupted, "previous_selection_preserved": True, "valid": True})
    report["negative_checks"] = negatives


def native_ui_acceptance(runner, root, output, report):
    root = Path(root)
    binary = root / "gui/build/v2d-main-frame-tests.exe"
    require(binary.is_file(), "Build this acceptance package with --include-native-tests")
    # Allow time for the full UI journey; individual job deadlines still apply.
    runner.command(root, "native-mainframe", [str(root)], raw=True, executable=binary, timeout=900)
    raw = Path(runner.commands[-1]["log_directory"]) / "stdout.log"
    paths = [line.removeprefix("Evidence: ").strip() for line in raw.read_text(encoding="utf-8").splitlines() if line.startswith("Evidence: ")]
    require(len(paths) == 1 and inside(paths[0], root / ".runs"), "Native UI test did not return its owned evidence directory")
    native = read_json(Path(paths[0]) / "summary.json")
    require(native.get("valid") is True, "Packaged native UI workflow failed")
    runner.command(root, "native-digital-open", [str(root), "--open-digital-only", str(Path(report["bundle"]["relocated"]) / "manifest.json")],
                   raw=True, executable=binary)
    raw = Path(runner.commands[-1]["log_directory"]) / "stdout.log"
    digital_paths = [line.removeprefix("Evidence: ").strip() for line in raw.read_text(encoding="utf-8").splitlines() if line.startswith("Evidence: ")]
    require(len(digital_paths) == 1 and inside(digital_paths[0], root / ".runs"), "Digital UI evidence escaped its owned directory")
    digital = read_json(Path(digital_paths[0]) / "summary.json")
    require(digital.get("valid") is True, "Packaged Open in Digital workflow failed")
    report["native_ui"] = {"workflows": native, "digital": digital, "physical_input_simulated": False}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", help="Comma-separated bounded subset; default all ten")
    parser.add_argument("--relocate", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--negative", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model", action="store_true", help="Verify/use the separately supplied offline Q4 runtime bundle")
    parser.add_argument("--ui", action="store_true", help="Run packaged native GUI acceptance (requires --model and test binaries)")
    args = parser.parse_args(argv)
    require(Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), "Run release acceptance with the repository .venv")
    require(os.name == "nt", "Native release acceptance currently targets Windows")
    require(not args.ui or args.model, "Full native UI acceptance requires the optional model bundle")
    package = args.package.resolve(strict=True)
    output = args.output.absolute()
    require(not output.exists() and output.parent.resolve(strict=True).is_relative_to(ROOT), "Choose a new workspace-owned evidence directory")
    require(str(output).isascii(), "Keep the acceptance state root ASCII; source/runtime Unicode paths are tested inside it")
    output.mkdir()
    report = {"schema_version": 1, "valid": False, "scope": "sanitized environment on current Windows host; not a clean VM",
              "platform": platform.platform(), "network": {"offline_flags": True, "network_disabled_by_test": False,
              "claim": "local verified artifacts only; no installation/download command is used"},
              "package": str(package), "installations": [], "model_requested": args.model, "ui_requested": args.ui}
    runner = Runner(output)
    try:
        files = runtime_files(package, models=args.model)
        copies = (2 if args.relocate else 0) + (1 if args.negative else 0)
        needed = copies * sum(item["size"] for item in files.values()) + 2 * 1024**3
        require(shutil.disk_usage(output).free >= needed, "Not enough free disk for isolated release copies and evidence")
        roots = [("original", package)]
        if args.relocate:
            installations = output / "installations"; installations.mkdir()
            roots += [("ascii", copy_files(package, installations / "ASCII installation", files)),
                      ("unicode", copy_files(package, installations / "Kurulum ışık π", files))]
        fixture_source = package / "examples/fixtures"
        fixture_files = {file.relative_to(fixture_source).as_posix(): None for file in fixture_source.rglob("*") if file.is_file()}
        projects = copy_files(fixture_source, output / "User projects ışık π", fixture_files)
        index = read_json(projects / "release/index.json")
        require(index["schema_version"] == 1 and len(index["cases"]) == 10, "Expected the fixed ten-project release corpus")
        selected = set(args.cases.split(",")) if args.cases else {case["id"] for case in index["cases"]}
        require(selected and selected <= {case["id"] for case in index["cases"]}, "Unknown release corpus case")
        report["cases"] = sorted(selected)
        baseline, fingerprints = {}, []
        for label, root in roots:
            doctor = runner.command(root, label + "-doctor", ["--doctor"])
            isolation = audit_isolation(doctor, root)
            fingerprints.append(isolation["runtime_fingerprint"])
            installation = {"label": label, "root": str(root), "diagnostics": isolation, "cases": {}}
            report["installations"].append(installation)
            for case in index["cases"]:
                if case["id"] not in selected:
                    continue
                project = contained(projects / "release", case["project"])
                destination = output / (label + "-" + case["id"])
                # Relative project paths must resolve from the launcher's original directory.
                relative_project = os.path.relpath(project, runner.cwd)
                result = runner.cli(root, label + "-" + case["id"], ["convert", "--project", relative_project,
                    "--output", destination, "--llm", "off", "--no-cache", "--seed", "0", "--random-vectors", "256"])
                audit = audit_conversion(result, case)
                installation["cases"][case["id"]] = audit
                if label == "original":
                    baseline[case["id"]] = audit
                else:
                    compare_native(baseline[case["id"]], audit)
            if baseline:
                previous = next(iter(baseline.values()))["manifest"]
                freshness = runner.cli(root, label + "-runtime-relocation-freshness", ["validate", previous])
                require(freshness.get("validation", freshness).get("valid") is True, "Identical runtime relocation broke original-source freshness")
            if args.model:
                model_acceptance(runner, root, label, projects, output, installation)
        require(len(set(fingerprints)) == 1, "Runtime content fingerprint changed after identical package relocation")
        package_relocation(runner, roots[-1][1], baseline, output, report)
        if args.negative:
            negative_acceptance(runner, package, roots[0][1], projects, baseline, output, report)
        if args.ui:
            native_ui_acceptance(runner, roots[-1][1], output, report)
        report["valid"] = True
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        report["commands"] = runner.commands
        report["owned_processes_zero"] = all(item.get("closed_metrics", {}).get("active_processes") == 0 for item in runner.commands)
        report["valid"] = report["valid"] and report["owned_processes_zero"]
        write_json(output / "summary.json", report)
        print(json.dumps({"valid": report["valid"], "summary": str(output / "summary.json"), "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
