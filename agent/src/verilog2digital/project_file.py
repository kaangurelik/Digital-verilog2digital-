"""Versioned project settings; relative paths belong to the project file."""
from __future__ import annotations

from pathlib import Path

from .contracts import validate
from .frontend.project import Project
from .jobs.publication import read_document


def load_project(path: Path) -> dict:
    path = Path(path).resolve(strict=True)
    value = read_document(path, limit=1024 * 1024)
    validate("project", value)
    def resolve(name):
        return (path.parent / name).resolve()
    project = Project([resolve(name) for name in value["sources"]], value.get("top"),
                      [resolve(name) for name in value.get("include_dirs", [])],
                      dict(value.get("defines", {})), dict(value.get("parameters", {}))).resolved()
    options = dict(value.get("conversion", {}))
    if "refinement" in options:
        from .llm.config import configuration
        options["refinement"] = configuration(options["refinement"])
    if "trace" in value:
        trace = read_document(resolve(value["trace"]), limit=1024 * 1024)
        if not isinstance(trace, dict):
            from .frontend.errors import FrontendError
            raise FrontendError("INVALID_TRACE", "A sequential trace must be a JSON object")
        options["sequential_trace"] = trace
    return {"path": path, "project": project, "options": options,
            "resources": dict(value.get("resources", {})),
            "output": resolve(value["output"]) if "output" in value else None}
