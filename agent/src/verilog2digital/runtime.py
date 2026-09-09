"""Development runtime discovery; all Python execution stays in the project venv."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def repository_root() -> Path:
    own = Path(__file__).resolve().parents[3]
    from .release import is_release
    # Packaged releases use their own tools, ignoring checkout overrides.
    if is_release(own):
        return own
    return Path(os.environ.get("V2D_ROOT", own)).resolve()


def require_venv(root: Path) -> None:
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != root / ".venv":
        raise RuntimeError(f"Use the project Python: {root / '.venv/Scripts/python.exe'}")
    from .release import is_release
    if is_release(root) and Path(sys.base_prefix).resolve() != root / ".tools/python-runtime":
        raise RuntimeError("Use the supplied portable launcher; the release venv must use its private Python base")


def development_toolchain(root: Path):
    """Share the tested setup paths without depending on the caller's cwd/PATH."""
    require_venv(root)
    path = root / "scripts/toolchain.py"
    spec = importlib.util.spec_from_file_location("v2d_development_toolchain", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Development toolchain is missing: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
