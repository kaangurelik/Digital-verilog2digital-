"""Explicit source order and frontend options, with no implicit shell parsing."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .errors import FrontendError


@dataclass
class Project:
    sources: list[Path]
    top: str | None = None
    include_dirs: list[Path] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, str | int] = field(default_factory=dict)

    def resolved(self) -> "Project":
        if not self.sources:
            raise FrontendError("NO_SOURCES", "Add at least one Verilog source file")
        sources = [Path(path).resolve() for path in self.sources]
        if len(sources) != len(set(sources)):
            raise FrontendError("DUPLICATE_SOURCE", "Each source file must appear only once")
        for path in sources:
            if not path.is_file():
                raise FrontendError("MISSING_SOURCE", f"Source file does not exist: {path}", path=str(path))
            if path.stat().st_size > 16 * 1024 * 1024:
                raise FrontendError("SOURCE_LIMIT", f"Source file exceeds the 16 MiB limit: {path}", path=str(path))
        includes = [Path(path).resolve() for path in self.include_dirs]
        for path in includes:
            if not path.is_dir():
                raise FrontendError("MISSING_INCLUDE_DIRECTORY", f"Include directory does not exist: {path}", path=str(path))
        if self.top is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", self.top):
            raise FrontendError("INVALID_TOP", "Top module must be an ordinary Verilog identifier")
        for name, value in self.defines.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                raise FrontendError("INVALID_DEFINE", f"Invalid macro name: {name}")
            if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
                raise FrontendError("INVALID_DEFINE", f"Macro {name} must have a single-line string value")
            if name in {"YOSYS", "SYNTHESIS"} and value != "1":
                raise FrontendError("RESERVED_DEFINE", f"The shared synthesis profile defines {name}=1")
        for name, value in self.parameters.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                raise FrontendError("INVALID_PARAMETER", f"Invalid parameter name: {name}")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise FrontendError("INVALID_PARAMETER", f"Parameter {name} must be an integer or integer literal")
        return Project(sources, self.top, includes, dict(self.defines), dict(self.parameters))

    def effective_defines(self) -> dict[str, str]:
        return {"YOSYS": "1", "SYNTHESIS": "1", **self.defines}
