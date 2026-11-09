"""Confine MCP path arguments to the directories the user allowed at startup."""
from __future__ import annotations

import os
from pathlib import Path
import re

from ..frontend.errors import FrontendError


MAX_PATH_CHARS = 4096
MAX_ALLOWED_DIRS = 32
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]|CONIN\$|CONOUT\$)(?:\..*)?\Z", re.IGNORECASE)
CONFIGURE = ("Restart the MCP server with --allow-dir <folder> (repeatable), for example "
             r"`v2d mcp --allow-dir C:\work\circuits` or `VerilogToDigital.exe --mcp --allow-dir C:\work\circuits`.")


class PathPolicy:
    """Resolve junctions and symbolic links, then require containment in an allowed root."""

    def __init__(self, directories=(), *, private=()):
        roots = []
        if len(directories) > MAX_ALLOWED_DIRS:
            raise FrontendError("ALLOW_DIR_LIMIT", f"At most {MAX_ALLOWED_DIRS} --allow-dir folders are supported")
        # Server-owned storage (job records, refinement scratch, the candidate cache) and the installation are never user paths.
        self.private = tuple(Path(os.path.realpath(Path(path).absolute())) for path in private)
        for item in directories:
            text = str(item)
            # An empty value (for example an unset host variable) must not become the working directory.
            if not text.strip() or "\x00" in text or len(text) > MAX_PATH_CHARS:
                raise FrontendError("ALLOW_DIR_INVALID", "An --allow-dir value is empty or blank, too long or contains NUL; "
                                    "give an existing folder as an absolute path")
            try:
                resolved = Path(os.path.realpath(Path(text).absolute(), strict=True))
            except OSError as error:
                raise FrontendError("ALLOW_DIR_INVALID", f"--allow-dir {text} does not exist or cannot be resolved: {error}",
                                    path=text) from error
            if not resolved.is_dir():
                raise FrontendError("ALLOW_DIR_INVALID", f"--allow-dir {text} is not a directory", path=text)
            if self.internal(resolved):
                raise FrontendError("ALLOW_DIR_INVALID", f"--allow-dir {text} is inside the server's own job storage, cache or "
                                    "installation; allow a folder that holds your designs", path=text)
            if resolved not in roots:
                roots.append(resolved)
        self.roots = tuple(roots)

    @property
    def configured(self):
        return bool(self.roots)

    def _inside(self, resolved) -> bool:
        return any(resolved == root or resolved.is_relative_to(root) for root in self.roots)

    def contains(self, path) -> bool:
        return self._inside(Path(os.path.realpath(Path(path).absolute())))

    def internal(self, path) -> bool:
        resolved = Path(os.path.realpath(Path(path).absolute()))
        return any(resolved == root or resolved.is_relative_to(root) for root in self.private)

    def _refuse(self, role, text, reason):
        allowed = ", ".join(str(root) for root in self.roots)
        return FrontendError("PATH_NOT_ALLOWED", f"The {role} {text!r} {reason}. Allowed directories: {allowed}. "
                             "Use a path inside one of them or restart the server with another --allow-dir.", path=text[:1024])

    def resolve(self, value, role, *, kind="file", write=False) -> Path:
        """Return a real path inside an allowed root; relative names use the first root."""
        if not self.roots:
            raise FrontendError("NO_ALLOWED_DIRECTORIES", f"This MCP server has no allowed directories, so the {role} "
                                f"cannot be read or written. {CONFIGURE}")
        text = str(value)
        if not text or len(text) > MAX_PATH_CHARS or any(char in text for char in "\x00\r\n"):
            raise FrontendError("INVALID_PATH", f"The {role} must be a nonempty single-line path of at most {MAX_PATH_CHARS} characters")
        if text.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\")):
            raise FrontendError("INVALID_PATH", f"The {role} uses a Windows device namespace prefix; use an ordinary path", path=text)
        path = Path(text)
        if path.drive and not path.is_absolute() or not path.drive and path.root:
            raise FrontendError("INVALID_PATH", f"The {role} {text!r} is relative to a drive or a drive root; "
                                "use an absolute path or a name relative to the first allowed directory", path=text)
        parts = path.parts[1:] if path.drive or path.root else path.parts
        if any(":" in part or DEVICE.match(part) or part.endswith((" ", ".")) and part not in {".", ".."} for part in parts):
            raise FrontendError("INVALID_PATH", f"The {role} {text!r} contains a stream name, reserved device name "
                                "or a trailing space or dot", path=text)
        if not path.is_absolute():
            path = self.roots[0] / path
        # Containment is decided before existence, so a refusal reveals nothing about files outside the allowed directories.
        try:
            located = Path(os.path.realpath(path))
        except (OSError, ValueError):
            located = Path(os.path.abspath(path))
        if not self._inside(located):
            raise self._refuse(role, text, "resolves outside the allowed directories")
        if self.internal(located):
            raise self._refuse(role, text, "is inside the server's own job storage, cache or installation")
        try:
            resolved = Path(os.path.realpath(path, strict=kind != "output"))
        except OSError as error:
            raise FrontendError("PATH_NOT_FOUND", f"The {role} {text!r} does not exist or cannot be resolved", path=text) from error
        if not self._inside(resolved):
            raise self._refuse(role, text, "resolves outside the allowed directories")
        if write and resolved in self.roots:
            raise self._refuse(role, text, "is an allowed directory itself; choose a subfolder for generated files")
        if self.internal(resolved):
            raise self._refuse(role, text, "is inside the server's own job storage, cache or installation")
        if kind == "file" and not resolved.is_file():
            raise FrontendError("PATH_NOT_FILE", f"The {role} {text!r} is not a regular file", path=text)
        if kind == "directory" and not resolved.is_dir():
            raise FrontendError("PATH_NOT_DIRECTORY", f"The {role} {text!r} is not a directory", path=text)
        if kind == "output" and resolved.exists() and not resolved.is_dir():
            raise FrontendError("PATH_NOT_DIRECTORY", f"The {role} {text!r} exists and is not a directory", path=text)
        return resolved

    def require(self, paths, role):
        """Check already resolved paths, such as recorded sources or include uses."""
        for path in paths:
            if not self.contains(path):
                raise self._refuse(role, str(path), "is outside the allowed directories")

    def describe(self):
        return {"configured": self.configured, "allowed_directories": [str(root) for root in self.roots],
                **({} if self.configured else {"configuration_hint": CONFIGURE})}
