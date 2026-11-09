"""Snapshot sources into ASCII paths for native Windows tools.

Each active include use gets a separate copy resolved through slang metadata.
Preserve preprocessing, source order and line counts for all three compilers.
Run inside the resource-limited worker.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from ..frontend.errors import FrontendError
from ..frontend.project import SLANG_MACROS, Project, private_macros

MAX_FILES = 1024
MAX_OCCURRENCES = 512
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_STAGED_BYTES = 128 * 1024 * 1024


def _configuration(project):
    return {"sources": [str(path) for path in project.sources],
            "include_dirs": [str(path) for path in project.include_dirs],
            "defines": project.effective_defines(), "parameters": project.parameters}


def _directives(paths, defines):
    """Snapshot native include-use ranges before releasing their source manager."""
    import pyslang as p
    manager = p.SourceManager()
    # Resolve Unicode includes in Python, then redirect them to ASCII copies.
    manager.setDisableLocalIncludes(True)
    # Track each __FILE__ include occurrence to recover its original location.
    manager.setDisableProximatePaths(True)
    for directory in sorted({str(Path(path).parent) for path in paths}):
        manager.addUserDirectories(directory)
    pp = p.parsing.PreprocessorOptions()
    pp.languageVersion = p.LanguageVersion.v1364_2005
    pp.predefines = [f"{name}={value}" for name, value in sorted(defines.items())]
    # Stage the includes that Yosys and Icarus read, which never see slang's own macros.
    pp.undefines = private_macros(SLANG_MACROS, defines)
    pp.maxIncludeDepth = 64
    parser = p.parsing.ParserOptions()
    parser.languageVersion = p.LanguageVersion.v1364_2005
    parser.maxRecursionDepth = 256
    tree = p.syntax.SyntaxTree.fromFiles([str(path) for path in paths], manager, p.Bag([pp, parser]))
    stack, found, seen, line_directives = [tree.root], [], set(), {}
    while stack:
        item = stack.pop()
        if isinstance(item, p.syntax.SyntaxNode):
            stack.extend(reversed(list(item)))
            continue
        if not isinstance(item, p.parsing.Token):
            continue
        for trivia in item.trivia:
            if trivia.kind.name != "Directive":
                continue
            directive = trivia.syntax()
            if directive is None:
                continue
            if directive.kind.name == "LineDirective":
                location = manager.getFullyExpandedLoc(directive.getFirstToken().location)
                path = str(Path(manager.getFullPath(location.buffer)).resolve())
                line_directives.setdefault(path, []).append(location.offset)
                continue
            if directive.kind.name != "IncludeDirective":
                continue
            location = directive.directive.location
            original_location = manager.getFullyExpandedLoc(location)
            path = str(Path(manager.getFullPath(original_location.buffer)).resolve())
            use_range = directive.fileName.range
            # Follow native __FILE__ origins through nested macros.
            # Leave ordinary literals intact, even when they resemble staged paths.
            spelling = manager.getFullyOriginalLoc(use_range.start)
            spelling_text = manager.getSourceText(spelling.buffer).encode("utf-8")
            spelling_token = spelling_text[spelling.offset:spelling.offset+10]
            file_macro = (spelling_token.startswith(b"`__FILE__")
                          and (len(spelling_token) == 9 or not (spelling_token[9:10].isalnum() or spelling_token[9:10] in {b"_", b"$"})))
            while manager.isMacroLoc(use_range.start):
                use_range = manager.getExpansionRange(use_range.start)
            signature = (path, original_location.offset, use_range.start.offset, use_range.end.offset)
            if signature in seen:
                continue
            seen.add(signature)
            supported = (manager.isFileLoc(location) and use_range.start.buffer == location.buffer
                         and use_range.end.buffer == location.buffer)
            found.append({"path": path, "directive_offset": original_location.offset,
                          "start": use_range.start.offset, "end": use_range.end.offset,
                          "filename": directive.fileName.rawText, "supported": supported,
                          "file_macro": file_macro,
                          "line": manager.getLineNumber(original_location),
                          "column": manager.getColumnNumber(original_location)})
    for item in found:
        item["line_remapped"] = any(offset < item["directive_offset"] for offset in line_directives.get(item["path"], []))
    errors, forbidden_uses = [], set()
    # Stringification can hide origins in final tokens; check expansion buffers.
    # Only include filenames may consume a path changed by staging.
    for buffer in manager.getAllBuffers():
        location = p.SourceLocation(buffer, 0)
        if not manager.isMacroLoc(location) or manager.getMacroName(location) != "__FILE__":
            continue
        location = manager.getFullyExpandedLoc(location)
        path = str(Path(manager.getFullPath(location.buffer)).resolve())
        if any(item["supported"] and item["path"] == path and item["start"] <= location.offset < item["end"] for item in found):
            continue
        key = (path, location.offset)
        if key in forbidden_uses:
            continue
        forbidden_uses.add(key)
        errors.append({"code": "UNSUPPORTED_SOURCE_LOCATION",
                       "message": "__FILE__ may only select an include filename; filename-dependent HDL values cannot be preserved by portable source staging",
                       "path": path, "line": manager.getLineNumber(location), "column": manager.getColumnNumber(location)})
    engine = p.DiagnosticEngine(manager)
    for diagnostic in tree.diagnostics:
        if engine.getSeverity(diagnostic.code, diagnostic.location).name.lower() not in {"error", "fatal"}:
            continue
        location = manager.getFullyExpandedLoc(diagnostic.location)
        errors.append({"code": "SLANG_ERROR", "message": engine.formatMessage(diagnostic),
                       "path": str(Path(manager.getFullPath(location.buffer)).resolve()),
                       "line": manager.getLineNumber(location), "column": manager.getColumnNumber(location)})
    return found, errors


def snapshot_project(project: Project, job_dir: Path) -> dict:
    """Copy sources and redirect active includes using native metadata.

    Configuration uses staged paths. Bindings and source maps retain original
    paths, hashes and column transforms. Writes stay in the new snapshot folder.
    """
    project = project.resolved()
    directory = Path(job_dir).resolve() / "sources"
    if os.name == "nt" and not str(directory).isascii():
        raise FrontendError("STAGING_PATH", "The native worker staging directory must have an ASCII path; choose an ASCII jobs directory", path=str(directory))
    directory.mkdir(parents=True, exist_ok=False)
    originals, copies, resolved_uses = {}, {}, set()
    source_bytes = staged_bytes = 0
    source_map = {"schema_version": 1, "files": {}, "configuration": _configuration(project)}

    def read_original(path):
        nonlocal source_bytes
        path = Path(path).resolve()
        key = str(path)
        if key in originals:
            return originals[key]
        if len(originals) >= MAX_FILES:
            raise FrontendError("SOURCE_SNAPSHOT_LIMIT", f"Source snapshot exceeds {MAX_FILES} distinct files", path=key)
        try:
            with path.open("rb") as file:
                data = file.read(MAX_FILE_BYTES+1)
        except OSError as error:
            raise FrontendError("SOURCE_READ_FAILED", f"Cannot snapshot source: {error}", path=key) from error
        if len(data) > MAX_FILE_BYTES or source_bytes + len(data) > MAX_SOURCE_BYTES:
            raise FrontendError("SOURCE_SNAPSHOT_LIMIT", "Source snapshot exceeds its 16 MiB per-file or 64 MiB total bound", path=key)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FrontendError("SOURCE_ENCODING", "Source text must use UTF-8 encoding", path=key) from error
        if b"\x00" in data:
            raise FrontendError("SOURCE_ENCODING", "Source text cannot contain NUL bytes", path=key)
        source_bytes += len(data)
        record = {"path": key, "data": data, "sha256": hashlib.sha256(data).hexdigest()}
        originals[key] = record
        return record

    def copy_occurrence(path):
        nonlocal staged_bytes
        if len(copies) >= MAX_OCCURRENCES:
            raise FrontendError("SOURCE_SNAPSHOT_LIMIT", f"Source snapshot exceeds {MAX_OCCURRENCES} source/include occurrences")
        original = read_original(path)
        if staged_bytes + len(original["data"]) > MAX_STAGED_BYTES:
            raise FrontendError("SOURCE_SNAPSHOT_LIMIT", "Expanded source snapshots exceed 128 MiB")
        staged_bytes += len(original["data"])
        target = directory / f"f{len(copies):04d}.v"
        target.write_bytes(original["data"])
        copies[str(target)] = {"original": original, "data": original["data"], "columns": []}
        source_map["files"][str(target)] = {"path": original["path"], "columns": copies[str(target)]["columns"]}
        return target

    paths = [copy_occurrence(path) for path in project.sources]
    for _ in range(MAX_OCCURRENCES + 1):
        directives, errors = _directives(paths, project.effective_defines())
        pending = [directive for directive in directives if directive["path"] in copies
                   and (directive["path"], directive["line"], directive["column"]) not in resolved_uses]
        if not pending:
            if errors:
                first = errors[0]
                raise restore_error(FrontendError(**first), source_map)
            break
        directive = pending[0]
        record = copies[directive["path"]]
        original_path = Path(record["original"]["path"])
        location = {"path": str(original_path), "line": directive["line"], "column": directive["column"]}
        filename = directive["filename"]
        if not directive["supported"] or not (len(filename) >= 2 and filename[0] == filename[-1] == '"'):
            raise FrontendError("UNSUPPORTED_INCLUDE_FORM", "Include filenames must resolve to one quoted literal or filename macro at a physical include directive; angle-bracket includes are outside the shared frontend profile", **location)
        name = filename[1:-1]
        if not name or "\x00" in name or "\n" in name or "\r" in name:
            raise FrontendError("UNSUPPORTED_INCLUDE_FORM", "Include filename is empty or contains a control character", **location)
        # __FILE__ inside an include must retain its original source meaning.
        if directive["file_macro"]:
            if directive["line_remapped"]:
                offset = directive["directive_offset"]
                data = record["data"]
                raise FrontendError("UNSUPPORTED_SOURCE_LOCATION",
                                    "__FILE__ includes following a `line directive have remapped filename semantics; use an explicit quoted include filename",
                                    path=str(original_path), line=data[:offset].count(b"\n")+1,
                                    column=offset-data.rfind(b"\n", 0, offset))
            name = str(original_path)
        candidate = Path(name)
        if candidate.is_absolute():
            choices = [candidate]
        else:
            choices = [original_path.parent / candidate, *(root / candidate for root in project.include_dirs)]
        selected = next((path.resolve() for path in choices if path.is_file()), None)
        if selected is None:
            raise FrontendError("MISSING_INCLUDE", f"Cannot resolve include {name!r} relative to this source or the ordered include directories", **location)
        start, end = directive["start"], directive["end"]
        data = record["data"]
        if not 0 <= start < end <= len(data) or b"\n" in data[start:end] or b"\r" in data[start:end]:
            raise FrontendError("UNSUPPORTED_INCLUDE_FORM", "Multiline or ambiguous include filename expansion is outside the snapshot profile", **location)
        target = copy_occurrence(selected)
        # The pinned Windows Icarus preprocessor does not recognize drive-letter
        # absolute include names. All copies share one private ASCII directory.
        replacement = ('"' + target.name + '"').encode("ascii")
        line = data[:start].count(b"\n") + 1
        column = start - data.rfind(b"\n", 0, start)
        if any(item["line"] == line for item in record["columns"]):
            raise FrontendError("UNSUPPORTED_INCLUDE_FORM", "Multiple include replacements on one physical line are unsupported", **location)
        record["columns"].append({"line": line, "start": column, "end": column+end-start,
                                  "new_end": column+len(replacement)})
        staged_bytes += len(replacement) - (end-start)
        if staged_bytes > MAX_STAGED_BYTES:
            raise FrontendError("SOURCE_SNAPSHOT_LIMIT", "Expanded source snapshots exceed 128 MiB")
        record["data"] = data[:start] + replacement + data[end:]
        Path(directive["path"]).write_bytes(record["data"])
        resolved_uses.add((directive["path"], directive["line"], directive["column"]))
    else:
        raise FrontendError("SOURCE_SNAPSHOT_LIMIT", "Include discovery exceeded its bounded pass count")
    sources = {key: value["sha256"] for key, value in sorted(originals.items())}
    for path, expected in sources.items():
        try:
            with Path(path).open("rb") as file:
                current = hashlib.sha256(file.read(MAX_FILE_BYTES+1)).hexdigest()
        except OSError as error:
            raise FrontendError("SOURCE_CHANGED", f"Source became unreadable during staging: {error}", path=path) from error
        if current != expected:
            raise FrontendError("SOURCE_CHANGED", "Source or include changed during staging; retry the job", path=path)
    binding = {"schema_version": 1, "language": "Verilog-2005", "source_order": [str(path) for path in project.sources],
               "files": [{"path": path, "sha256": value["sha256"], "bytes": len(value["data"])} for path, value in sorted(originals.items())],
               "staged": [{"path": path, "original_path": value["original"]["path"],
                           "sha256": hashlib.sha256(value["data"]).hexdigest(), "bytes": len(value["data"]),
                           "original_sha256": value["original"]["sha256"], "columns": value["columns"]}
                          for path, value in sorted(copies.items())]}
    staged = Project(paths, project.top, [directory], dict(project.defines), dict(project.parameters))
    (directory / "snapshot.json").write_text(json.dumps({"binding": binding, "source_map": source_map}, indent=2, sort_keys=True), encoding="utf-8")
    return {"project": staged, "sources": sources, "binding": binding, "source_map": source_map}


def verify_snapshot(snapshot: dict) -> None:
    """Require every staged byte to match the recorded immutable input binding."""
    binding = snapshot.get("binding", snapshot)
    if binding.get("schema_version") != 1 or not isinstance(binding.get("staged"), list) or not 1 <= len(binding["staged"]) <= MAX_OCCURRENCES:
        raise FrontendError("SOURCE_SNAPSHOT_INVALID", "Source snapshot has an invalid binding")
    seen = set()
    for record in binding["staged"]:
        path = Path(record["path"])
        if str(path) in seen or type(record["bytes"]) is not int or not 0 <= record["bytes"] <= MAX_FILE_BYTES + 65536:
            raise FrontendError("SOURCE_SNAPSHOT_INVALID", "Source snapshot has duplicate paths or unbounded file sizes")
        seen.add(str(path))
        try:
            with path.open("rb") as file:
                data = file.read(record["bytes"]+1)
        except OSError as error:
            raise FrontendError("SOURCE_SNAPSHOT_CHANGED", f"Staged source became unreadable: {error}", path=str(path)) from error
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise FrontendError("SOURCE_SNAPSHOT_CHANGED", "A staged source changed after its input snapshot was recorded", path=str(path))


def _column(mapping, line, column):
    if type(line) is not int or type(column) is not int:
        return column
    for span in mapping.get("columns", []):
        if span["line"] == line and column >= span["start"]:
            if column >= span["new_end"]:
                return column + span["end"] - span["new_end"]
            return span["start"] + min(column - span["start"], span["end"] - span["start"])
    return column


def _source_text(text, source_map):
    if not isinstance(text, str):
        return text
    for path, mapping in sorted(source_map.get("files", {}).items(), key=lambda item: -len(item[0])):
        for spelling in {path, Path(path).as_posix()}:
            if spelling not in text:
                continue
            # Yosys source spans carry byte columns; only rewrite recognized spans.
            pattern = re.compile(re.escape(spelling) + r":(\d+)\.(\d+)-(\d+)\.(\d+)")
            def replace(match):
                first_line, first_column, last_line, last_column = map(int, match.groups())
                return f"{mapping['path']}:{first_line}.{_column(mapping, first_line, first_column)}-{last_line}.{_column(mapping, last_line, last_column)}"
            text = pattern.sub(replace, text)
            text = text.replace(spelling, mapping["path"])
    return text


def restore_provenance(value, source_map: dict):
    """Return a JSON copy with original configuration and recognized source paths."""
    files = source_map.get("files", {})
    def visit(item, key=None):
        if key in {"attributes", "parameters", "parameters_raw", "defines", "native_name", "hierarchy", "hdl_path"}:
            return deepcopy(item)
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if isinstance(item, dict):
            if key == "configuration" and set(item) == {"sources", "include_dirs", "defines", "parameters"}:
                return deepcopy(source_map["configuration"])
            result = {name: visit(child, name) for name, child in item.items()}
            mapping = files.get(item.get("path")) if isinstance(item.get("path"), str) else None
            if mapping:
                result["path"] = mapping["path"]
                if "column" in item:
                    result["column"] = _column(mapping, item.get("line"), item["column"])
                if "end_column" in item:
                    result["end_column"] = _column(mapping, item.get("end_line"), item["end_column"])
            return result
        if isinstance(item, str):
            if key in {"path", "sources", "source_files", "include_dirs", "original_path"} and item in files:
                return files[item]["path"]
            if key in {"source", "message"}:
                return _source_text(item, source_map)
        return deepcopy(item)
    return visit(value)


def restore_error(error: FrontendError, source_map: dict) -> FrontendError:
    result = restore_provenance(error.as_dict(), source_map)
    return FrontendError(**result)


def inspect_project(project: Project, *, discover: bool = True) -> dict:
    """Inspect an already staged project, including structured ambiguous tops."""
    from ..frontend.inspection import inspect_sources
    from ..frontend.yosys import _parameter
    project = project.resolved()
    parameters = {name: _parameter(value) for name, value in project.parameters.items()}
    return inspect_sources(project.sources, top=project.top, include_dirs=project.include_dirs,
                           defines=project.effective_defines(), parameters=parameters, discover=discover)
