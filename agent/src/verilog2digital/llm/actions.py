"""Untrusted model responses can propose geometry, never electrical changes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from ..contracts import ContractError, validate, validate_circuit
from ..frontend.errors import FrontendError
from ..mapping import validate_mapped
from ..runtime import repository_root


MAX_RESPONSE_BYTES = 16 * 1024
GRID = 20
MAX_DELTA = 2000
MAX_COORDINATE = 1_000_000


class ActionError(FrontendError):
    """A rejected model proposal, compatible with structured job diagnostics."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def action_schema() -> dict:
    """A fresh JSON schema for the server; local validation remains mandatory."""
    return json.loads((repository_root() / "schemas/layout-actions.schema.json").read_text(encoding="utf-8"))


def _baseline(mapped, layout):
    try:
        validate_mapped(mapped)
        validate_circuit(layout["circuit"])
        instances = layout["circuit"]["instances"]
        intended = {item["id"]: item for item in mapped["instances"]}
        if len(instances) != len(intended) or {item["id"] for item in instances} != set(intended):
            raise ValueError("The layout must contain exactly the mapped components on this canvas")
        for item in instances:
            expected = intended[item["id"]]
            if any(item[field] != expected[field] for field in ("type", "attributes", "rotation")):
                raise ValueError("Layout component types, attributes and rotations must match immutable mapping")
            if any(type(value) is not int or value % GRID for value in item["position"].values()):
                raise ValueError("Component positions must use the 20-unit placement grid")
        descriptions = layout["descriptors"]
        if len(descriptions) != len(instances) or {item["id"] for item in descriptions} != set(intended):
            raise ValueError("Every placed component requires its exact native geometry")
        return instances
    except (FrontendError, ContractError, ValueError, KeyError, TypeError) as error:
        raise ActionError("ACTION_BASELINE", str(error)) from error


def candidate_id(mapped: dict, layout: dict) -> str:
    """Bind intent and current geometry/order; exclude source paths and timings.

    Source/runtime fingerprints are separately bound by the job and replay log.
    Excluding source origins lets an identical canvas be replayed after staging.
    """
    _baseline(mapped, layout)
    fields = ("profile", "top", "module_id", "instances", "nets", "pin_bindings", "inputs", "outputs", "clock", "registers", "child_types")
    intent = {name: mapped[name] for name in fields if name in mapped}
    # Register source strings describe provenance, not native state semantics.
    if "registers" in intent:
        intent["registers"] = [{key: value for key, value in item.items() if key not in {"source", "hierarchy", "native_name"}}
                               for item in intent["registers"]]
    bound = {"schema_version": 1, "intent": intent, "circuit": layout["circuit"],
             "descriptors": layout["descriptors"], "spacing": layout.get("spacing", 40)}
    return hashlib.sha256(_canonical(bound)).hexdigest()


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("A JSON object contains a duplicate field")
        result[name] = value
    return result


def parse_actions(raw: str | bytes, *, mapped: dict, layout: dict, expected_candidate_id: str | None = None,
                  visible_instance_ids=None, visible_net_ids=None) -> dict:
    """Parse exactly one bounded JSON response; never salvage prose or fences."""
    if not isinstance(raw, (str, bytes)):
        raise ActionError("ACTION_JSON", "Model output must be UTF-8 JSON text")
    try:
        data = raw.encode("utf-8", errors="strict") if isinstance(raw, str) else raw
        if len(data) > MAX_RESPONSE_BYTES:
            raise ActionError("ACTION_LIMIT", "Model output exceeds the 16 KiB response limit")
        batch = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_fields,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    except ActionError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ActionError("ACTION_JSON", "Model output must be one valid JSON object with unique fields and finite numbers") from error
    validate_actions(batch, mapped=mapped, layout=layout, expected_candidate_id=expected_candidate_id,
                     visible_instance_ids=visible_instance_ids, visible_net_ids=visible_net_ids)
    return batch


def validate_actions(batch: dict, *, mapped: dict, layout: dict, expected_candidate_id: str | None = None,
                     visible_instance_ids=None, visible_net_ids=None) -> None:
    # Reject lone surrogates in decoded JSON keys and values before writing reports.
    try:
        size = 0
        for fragment in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(batch):
            size += len(fragment.encode("utf-8", errors="strict"))
            if size > MAX_RESPONSE_BYTES:
                raise ActionError("ACTION_LIMIT", "Model output exceeds the 16 KiB response limit")
    except ActionError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ActionError("ACTION_JSON", "Model response must contain valid Unicode text and finite JSON values") from error
    try:
        validate("layout-actions", batch)
    except (ContractError, RecursionError, TypeError) as error:
        # Do not echo the untrusted payload (or megabytes of schema context).
        raise ActionError("ACTION_SCHEMA", "Model response does not match the bounded layout action schema") from error
    if type(batch["schema_version"]) is not int:
        raise ActionError("ACTION_SCHEMA", "The action schema version must be an integer")
    current = candidate_id(mapped, layout)
    if batch["candidate_id"] != current or (expected_candidate_id is not None and expected_candidate_id != current):
        raise ActionError("ACTION_STALE", "The proposal refers to a different layout candidate")
    if any(ord(char) < 32 and char not in "\n\t" for char in batch["explanation"]):
        raise ActionError("ACTION_SCHEMA", "Proposal explanations cannot contain control characters")
    positions = {item["id"]: item["position"] for item in layout["circuit"]["instances"]}
    nets = {item["id"] for item in mapped["nets"]}
    movable = set(positions) if visible_instance_ids is None else set(visible_instance_ids)
    visible_nets = nets if visible_net_ids is None else set(visible_net_ids)
    if not movable <= set(positions) or not visible_nets <= nets:
        raise ActionError("ACTION_SCOPE", "The proposal context contains unknown component or net IDs")
    moved, rerouted = set(), False
    for action in batch["actions"]:
        if action["action"] == "reroute_nets":
            if rerouted:
                raise ActionError("ACTION_CONFLICT", "A batch may specify routing priorities only once")
            if not set(action["net_ids"]) <= nets:
                raise ActionError("ACTION_UNKNOWN_ID", "A routing action references an unknown net")
            if not set(action["net_ids"]) <= visible_nets:
                raise ActionError("ACTION_SCOPE", "A routing action references a net outside the visible context")
            rerouted = True
            continue
        identifiers = [action["instance_id"]] if action["action"] == "move_component" else action["instance_ids"]
        if not set(identifiers) <= set(positions):
            raise ActionError("ACTION_UNKNOWN_ID", "A move references an unknown component")
        if not set(identifiers) <= movable:
            raise ActionError("ACTION_SCOPE", "A move references a component outside the visible context")
        if moved.intersection(identifiers):
            raise ActionError("ACTION_CONFLICT", "Each component may be moved only once per batch")
        delta = action["delta"]
        if any(type(value) is not int or abs(value) > MAX_DELTA or value % GRID for value in delta.values()):
            raise ActionError("ACTION_RANGE", "Move deltas must be integer multiples of 20 within plus or minus 2000")
        if not any(delta.values()):
            raise ActionError("ACTION_NO_CHANGE", "A move must change at least one coordinate; use an empty action list for no proposal")
        if any(abs(positions[identifier][axis] + delta[axis]) > MAX_COORDINATE for identifier in identifiers for axis in ("x", "y")):
            raise ActionError("ACTION_RANGE", "A resulting component position exceeds Digital coordinate bounds")
        moved.update(identifiers)


def apply_actions(batch: dict, mapped: dict, layout: dict) -> dict:
    """Return routing inputs with the baseline instance order intact.

    The router checks bounds and reroutes every net; native reload then verifies
    electrical connectivity.
    """
    validate_actions(batch, mapped=mapped, layout=layout)
    positions = {item["id"]: deepcopy(item["position"]) for item in layout["circuit"]["instances"]}
    priority_nets, reverse = [], False
    for action in batch["actions"]:
        if action["action"] == "reroute_nets":
            priority_nets = list(action["net_ids"])
            reverse = action["order"] == "reverse"
        else:
            identifiers = [action["instance_id"]] if action["action"] == "move_component" else action["instance_ids"]
            for identifier in identifiers:
                positions[identifier] = {axis: positions[identifier][axis] + action["delta"][axis] for axis in ("x", "y")}
    return {"positions": positions, "priority_nets": priority_nets, "reverse": reverse}
