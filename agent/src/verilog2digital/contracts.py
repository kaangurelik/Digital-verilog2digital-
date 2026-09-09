"""Versioned JSON contracts at the Python/Java boundary."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .runtime import repository_root


class ContractError(ValueError):
    pass


@lru_cache(maxsize=16)
def _validator(name: str, root: str) -> Draft202012Validator:
    path = Path(root) / "schemas" / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(name: str, value, root: Path | None = None) -> None:
    error = next(_validator(name, str(root or repository_root())).iter_errors(value), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractError(f"{name} at {location}: {error.message}")


def validate_circuit(circuit: dict, root: Path | None = None) -> None:
    validate("circuit", circuit, root)
    ids = [instance["id"] for instance in circuit["instances"]]
    if len(ids) != len(set(ids)):
        raise ContractError("Circuit instance IDs must be unique")
    for wire in circuit["wires"]:
        a, b = wire["from"], wire["to"]
        if a == b or (a["x"] != b["x"] and a["y"] != b["y"]):
            raise ContractError("Wires must be nonzero orthogonal segments")
