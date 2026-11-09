"""Fast optional-model inventory; full cryptographic checks happen before loading."""
from pathlib import Path

from ..runtime import repository_root
from .artifacts import load_lock, model_path


def inventory(root=None):
    root = Path(root or repository_root()).resolve()
    lock = load_lock(root)
    models = []
    for identifier, spec in lock["models"].items():
        path = model_path(identifier, root)
        try:
            present = path.is_file() and path.stat().st_size == spec["size"]
        except OSError:
            present = False
        models.append({"model_id": identifier, "repository": spec["repository"],
                       "revision": spec["revision"], "filename": spec["filename"],
                       "download_bytes": spec["size"], "sha256": spec["sha256"],
                       "files_present": present, "verified": False})
    runtimes = {backend: (root / ".tools/llama.cpp" / (lock["runtime"]["nightly"] + "-" + backend)
                         / "installation.json").is_file() for backend in ("cuda", "cpu")}
    return {"schema_version": 1, "state": "ready", "models": models,
            "runtime_markers_present": runtimes, "runtime_release": lock["runtime"]["release"],
            "scope": "file_presence_and_size; all hashes are verified before inference",
            "automatic_model": "qwen3-4b-q4_k_m"}
