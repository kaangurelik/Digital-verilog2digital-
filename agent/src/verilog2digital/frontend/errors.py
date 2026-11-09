"""Actionable HDL diagnostics shared by inspection and lowering."""
from pathlib import Path
import re


class FrontendError(ValueError):
    def __init__(self, code: str, message: str, *, path: str | None = None,
                 line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.line = line
        self.column = column

    def as_dict(self) -> dict:
        return {key: value for key, value in {"code": self.code, "message": self.message,
                                            "path": self.path, "line": self.line,
                                            "column": self.column}.items() if value is not None}


def source_location(source) -> dict:
    """Return path/line/column keywords for the first Yosys src span, if any.

    Yosys writes path:line.column-line.column and joins merged spans with '|'.
    """
    if not isinstance(source, str):
        return {}
    match = re.fullmatch(r"(.+?):([0-9]+)(?:\.([0-9]+))?(?:-[0-9]+(?:\.[0-9]+)?)?", source.split("|")[0].strip())
    if match is None:
        return {}
    path, line, column = match.groups()
    return {"path": str(Path(path)), "line": int(line), **({"column": int(column)} if column else {})}
