"""Actionable HDL diagnostics shared by inspection and lowering."""


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
