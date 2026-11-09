"""Bind native PNG transforms and geometry to relocatable, immutable artifacts."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import struct
import zlib

from .contracts import ContractError, validate
from .frontend.errors import FrontendError
from .jobs.publication import atomic_document, digest, read_document


MAX_METADATA = 8 * 1024**2


def _error(message):
    return FrontendError("PREVIEW_METADATA", message)


def png_dimensions(path):
    with Path(path).open("rb") as stream:
        header = stream.read(33)
    if (len(header) != 33 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or zlib.crc32(header[12:29]) != struct.unpack(">I", header[29:33])[0]):
        raise _error("Preview has no valid bounded PNG header")
    width, height = struct.unpack(">II", header[16:24])
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or width * height > 16_000_000:
        raise _error("PNG dimensions exceed the native preview budget")
    return width, height


def validate_metadata(value):
    try:
        validate("preview-metadata", value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ContractError, ValueError, TypeError, RecursionError) as error:
        raise _error("Invalid preview metadata: " + str(error)[:1000]) from error
    if len(encoded) > MAX_METADATA:
        raise _error("Preview metadata exceeds 8 MiB")
    for name, view in value["views"].items():
        if len(json.dumps(view, ensure_ascii=False).encode("utf-8")) > 4 * 1024**2:
            raise _error("One preview canvas exceeds 4 MiB")
        if view["image"]["artifact"] != ("preview" if name == "selected" else "baseline_preview"):
            raise _error("Preview role does not match its image artifact")
        if (name == "selected") != (view["comparison_scope"] == "selected_top_canvas"):
            raise _error("Preview role does not match its comparison scope")
        image = view["image"]
        if image["width_px"] * image["height_px"] > 16_000_000:
            raise _error("Preview dimensions exceed the pixel budget")
        affine = view["circuit_to_pixel"]
        if (any(not math.isfinite(item) for item in affine.values()) or affine["b"] != 0 or affine["c"] != 0
                or not .049999 <= affine["a"] <= 8 or affine["a"] != affine["d"]
                or abs(affine["tx"]) > 16_000_000 or abs(affine["ty"]) > 16_000_000):
            raise _error("Preview transform does not describe the native PNG renderer")
        instances = view["instances"]
        if len({item["id"] for item in instances}) != len(instances) or sum(len(item["pins"]) for item in instances) > 16384:
            raise _error("Preview instance identities or pin budget are invalid")
        for item in instances:
            if len({(pin["direction"], pin["pin"]) for pin in item["pins"]}) != len(item["pins"]):
                raise _error("Preview repeats a pin identity")
            for bounds in (item["bounds"], item["body_bounds"]):
                if any(bounds["min"][axis] > bounds["max"][axis] for axis in ("x", "y")):
                    raise _error("Preview component bounds are inverted")
    if "baseline" in value["views"]:
        selected, baseline = (value["views"][key] for key in ("selected", "baseline"))
        def identity(view):
            return [(item["id"], item["type"], item["label"], item["rotation"],
                     [(pin["direction"], pin["pin"], pin["width"]) for pin in item["pins"]]) for item in view["instances"]]
        if selected["module_id"] != baseline["module_id"] or identity(selected) != identity(baseline):
            raise _error("Baseline and selected previews do not share immutable instance identities")


def render_preview(adapter, paths, output, *, inspection, emitted, scale, module_id=None,
                   comparison_scope="selected_top_canvas", artifact="preview"):
    hashes = {"sha256": emitted["sha256"], "sidecar_sha256": emitted["sidecar_sha256"]}
    if any(digest(paths[key]) != hashes[hash_key] for key, hash_key in (("path", "sha256"), ("sidecar_path", "sidecar_sha256"))):
        raise FrontendError("ARTIFACT_CHANGED", "Native files changed before rendering their overlays")
    response = adapter.call("circuit.render", {**paths, "format": "png", "output_path": str(output), "scale": scale})
    native = response.get("metadata")
    if not isinstance(native, dict):
        raise _error("The native renderer did not return preview metadata")
    view = {**deepcopy(native), "module_id": module_id, "comparison_scope": comparison_scope}
    view["image"]["artifact"] = artifact
    # Validate a baseline through the same schema before the selected canvas exists.
    check = deepcopy(view)
    check["image"]["artifact"], check["comparison_scope"] = "preview", "selected_top_canvas"
    validate_metadata({"schema_version": 1, "top": "pending", "views": {"selected": check}})
    if view["circuit"] != hashes or digest(output) != view["image"]["sha256"]:
        raise _error("Rendered preview hashes do not match the validated native snapshot")
    if png_dimensions(output) != (view["image"]["width_px"], view["image"]["height_px"]):
        raise _error("Rendered PNG dimensions disagree with its native transform")
    actual, expected = view["instances"], inspection["instances"]
    if len(actual) != len(expected):
        raise _error("Rendered component count differs from native inspection")
    for item, inspected in zip(actual, expected):
        if (any(item[key] != inspected[key] for key in ("id", "type", "position", "rotation", "bounds", "body_bounds"))
                or item["label"] != inspected.get("attributes", {}).get("Label", "")
                or len(item["pins"]) != len(inspected["pins"])):
            raise _error("Rendered component geometry differs from native inspection")
        for pin, expected_pin in zip(item["pins"], inspected["pins"]):
            if (any(pin[key] != expected_pin[key] for key in ("direction", "pin", "position"))
                    or pin["width"] is not None and pin["width"] != expected_pin["width"]):
                raise _error("Rendered pin geometry differs from native inspection")
    if any(digest(paths[key]) != hashes[hash_key] for key, hash_key in (("path", "sha256"), ("sidecar_path", "sidecar_sha256"))):
        raise FrontendError("ARTIFACT_CHANGED", "Native files changed while rendering their overlays")
    return view


def write_metadata(path, top, selected, baseline=None):
    value = {"schema_version": 1, "top": top, "views": {"selected": selected}}
    if baseline is not None:
        value["views"]["baseline"] = baseline
    validate_metadata(value)
    if len(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")) + 1 > MAX_METADATA:
        raise _error("Serialized preview metadata exceeds 8 MiB")
    atomic_document(path, value)


def verify_preview_bindings(manifest, bindings):
    """Pure publication/cache checks; old image-only bundles remain readable."""
    if "preview_metadata" not in bindings:
        return
    value = read_document(bindings["preview_metadata"][0], limit=MAX_METADATA)
    validate_metadata(value)
    if value["top"] != manifest["top"]:
        raise _error("Preview belongs to a different top module")
    if ("baseline" in value["views"]) != ("baseline_preview" in bindings):
        raise _error("Baseline image and metadata must be published together")
    for view in value["views"].values():
        image = view["image"]
        if image["artifact"] not in bindings or bindings[image["artifact"]][1] != image["sha256"]:
            raise _error("Preview metadata identifies a different image")
        if png_dimensions(bindings[image["artifact"]][0]) != (image["width_px"], image["height_px"]):
            raise _error("Preview metadata dimensions differ from the image")
    selected = value["views"]["selected"]
    if selected["circuit"] != {"sha256": bindings["circuit"][1], "sidecar_sha256": bindings["sidecar"][1]}:
        raise _error("Preview metadata identifies a different native circuit")
    sidecar = read_document(bindings["sidecar"][0])
    if ([item["id"] for item in selected["instances"]] != [item["id"] for item in sidecar["instances"]]
            or sidecar["sha256"] != selected["circuit"]["sha256"]):
        raise _error("Preview IDs differ from the checked native sidecar")
