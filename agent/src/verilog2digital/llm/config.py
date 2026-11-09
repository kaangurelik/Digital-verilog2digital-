"""Small, serializable refinement settings shared by jobs and the desktop client."""
from __future__ import annotations

from copy import deepcopy
import math

from ..contracts import ContractError, validate
from ..frontend.errors import FrontendError


DEFAULTS = {
    "mode": "off", "model_id": "qwen3-4b-q4_k_m", "max_attempts": 3,
    "context_tokens": None, "model_memory_mib": 6144, "idle_seconds": 30,
    "budget_seconds": 120, "request": "Reduce area, wire length, bends and crossings while preserving spacing.",
    "responses": [],
}


def configuration(value=None) -> dict:
    if value is None:
        return deepcopy(DEFAULTS)
    try:
        validate("refinement", value)
    except ContractError as error:
        raise FrontendError("INVALID_REFINEMENT", str(error)[:2000]) from error
    result = {**deepcopy(DEFAULTS), **deepcopy(value)}
    if (type(result["max_attempts"]) is not int
            or type(result["model_memory_mib"]) is not int
            or type(result["idle_seconds"]) is not int
            or result["context_tokens"] is not None and type(result["context_tokens"]) is not int
            or isinstance(result["budget_seconds"], bool) or not math.isfinite(result["budget_seconds"])):
        raise FrontendError("INVALID_REFINEMENT", "Refinement requires integer attempt, context, memory and idle limits and a finite time budget")
    if result["mode"] != "replay" and result["responses"]:
        raise FrontendError("INVALID_REFINEMENT", "Recorded responses require replay mode")
    try:
        for text in [result["request"], *result["responses"]]:
            text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FrontendError("INVALID_REFINEMENT", "Refinement text must contain valid Unicode scalar values") from error
    return result
