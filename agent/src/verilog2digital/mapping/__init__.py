"""Map validated HDL bit identities to native Digital component pins."""

from .scalar import MappingError, map_scalar, validate_mapped as _validate_scalar
from .datapath import map_datapath, validate_datapath
from .sequential import map_sequential, validate_sequential
from .hierarchy import map_hierarchy_module, validate_hierarchy_mapping


def map_design(design, adapter, profile="auto"):
    """Choose scalar when supported, otherwise use the readable datapath profile."""
    if profile == "scalar-v1":
        return map_scalar(design, adapter)
    if profile == "datapath-v1":
        return map_datapath(design, adapter)
    if profile == "sequential-v1":
        return map_sequential(design, adapter)
    if profile != "auto":
        raise MappingError("UNSUPPORTED_MAPPING_PROFILE", f"Unknown mapping profile {profile!r}.")
    if isinstance(design, dict) and isinstance(design.get("cells"), list) and any(isinstance(cell, dict) and cell.get("type") == "$dff" for cell in design["cells"]):
        return map_sequential(design, adapter)
    try:
        return map_scalar(design, adapter)
    except MappingError as error:
        if error.code not in {"UNSUPPORTED_MAPPING_WIDTH", "UNSUPPORTED_MAPPING_CELL", "MAPPING_RESOURCE_LIMIT"}:
            raise
    return map_datapath(design, adapter)


def validate_mapped(mapped):
    """Validate the electrical contract selected by the explicit profile field."""
    if isinstance(mapped, dict) and mapped.get("profile") == "hierarchy-v1":
        return validate_hierarchy_mapping(mapped)
    if isinstance(mapped, dict) and mapped.get("profile") == "datapath-v1":
        return validate_datapath(mapped)
    if isinstance(mapped, dict) and mapped.get("profile") == "sequential-v1":
        return validate_sequential(mapped)
    return _validate_scalar(mapped)


__all__ = ["MappingError", "map_scalar", "map_datapath", "map_sequential", "map_design", "validate_mapped", "validate_datapath", "validate_sequential", "map_hierarchy_module", "validate_hierarchy_mapping"]
