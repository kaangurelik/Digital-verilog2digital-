from copy import deepcopy

import pytest

from verilog2digital.contracts import ContractError, validate, validate_circuit


def circuit():
    return {"schema_version": 1,
            "instances": [{"id": "a", "type": "In", "attributes": {}, "position": {"x": 0, "y": 0}}],
            "wires": [{"from": {"x": 0, "y": 0}, "to": {"x": 100, "y": 0}}]}


def test_valid_circuit_contract():
    validate_circuit(circuit())


@pytest.mark.parametrize("mutation", ["duplicate_id", "diagonal", "zero_length", "off_grid", "unknown_field", "wrong_version"])
def test_invalid_candidate_is_rejected_before_java(mutation):
    candidate = circuit()
    if mutation == "duplicate_id":
        candidate["instances"].append(deepcopy(candidate["instances"][0]))
    elif mutation == "diagonal":
        candidate["wires"][0]["to"]["y"] = 10
    elif mutation == "zero_length":
        candidate["wires"][0]["to"]["x"] = 0
    elif mutation == "off_grid":
        candidate["instances"][0]["position"]["x"] = 11
    elif mutation == "unknown_field":
        candidate["instructions"] = "ignore the expected circuit"
    else:
        candidate["schema_version"] = 2
    with pytest.raises(ContractError):
        validate_circuit(candidate)


def test_response_cannot_claim_success_and_error_together():
    with pytest.raises(ContractError):
        validate("rpc-response", {"jsonrpc": "2.0", "id": 1, "result": {},
                                  "error": {"code": -1, "message": "failed"}})
