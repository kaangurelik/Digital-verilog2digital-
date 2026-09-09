"""Check bit connectivity through native wires and transparent splitter ports."""
from __future__ import annotations

from collections import defaultdict

from .topology import endpoint_key


def compare_bit_topology(mapped: dict, inspection: dict) -> dict:
    """Reconstruct native bit groups and compare electrical identities.

    Wires preserve bit order; splitters join bits using reloaded offsets.
    Component behavior is checked separately.
    """
    errors = []
    try:
        if inspection.get("topology_available") is False:
            raise ValueError("Digital did not return native topology")
        instances = {item["id"]: item for item in inspection["instances"]}
        if len(instances) != len(inspection["instances"]):
            raise ValueError("Native component IDs must be unique")
        widths = {}
        for identifier, item in instances.items():
            for pin in item["pins"]:
                key = (identifier, pin["direction"], pin["pin"])
                width = pin.get("width")
                if key in widths or type(width) is not int or not 1 <= width <= 64:
                    raise ValueError(f"Invalid or unresolved native width for {key}")
                widths[key] = width
        expected = {}
        for net in mapped["nets"]:
            bits = net.get("bits", [net.get("bit")])
            if len(bits) != net["width"] or any(
                    not (type(bit) is int and bit >= 2 or type(bit) is str and bit in {"0", "1"}) for bit in bits):
                raise ValueError(f"Invalid bit vector for {net['id']}")
            for endpoint in net["endpoints"]:
                key = endpoint_key(endpoint)
                if key in expected or widths.get(key) != len(bits):
                    raise ValueError(f"Duplicate, missing, or width-mismatched endpoint {key}")
                expected[key] = bits
        if set(expected) != set(widths):
            raise ValueError("Expected bit bindings must cover every native pin exactly once")
        if "pin_bindings" in mapped:
            bindings = {}
            for binding in mapped["pin_bindings"]:
                key = endpoint_key(binding)
                if key in bindings or binding["width"] != len(binding["bits"]):
                    raise ValueError("Expected per-pin bit bindings contain duplicate pins or invalid widths")
                bindings[key] = binding["bits"]
            if bindings != expected:
                raise ValueError("Mapped net bits differ from the independently recorded per-pin bit bindings")
        parents = {(key, index): (key, index) for key, width in widths.items() for index in range(width)}

        def find(node):
            while parents[node] != node:
                parents[node] = parents[parents[node]]
                node = parents[node]
            return node

        def union(first, second):
            a, b = find(first), find(second)
            if a != b:
                parents[b] = a

        covered = set()
        groups = [net["endpoints"] for net in inspection["nets"]]
        groups.extend([[pin] for pin in inspection["unconnected"]])
        for endpoints in groups:
            keys = [endpoint_key(endpoint) for endpoint in endpoints]
            if not keys or any(key not in widths or key in covered for key in keys) or len(keys) != len(set(keys)):
                raise ValueError("Native nets contain missing, duplicate, or unknown endpoints")
            covered.update(keys)
            if len({widths[key] for key in keys}) != 1:
                raise ValueError("A native wire joins pins of different widths")
            for key in keys[1:]:
                for index in range(widths[key]):
                    union((keys[0], index), (key, index))
        if covered != set(widths):
            raise ValueError("Native topology omitted pins")
        for identifier, item in instances.items():
            if item["type"] != "Splitter":
                continue
            offsets = defaultdict(list)
            seen = set()
            for binding in item["splitter_bindings"]:
                key = (identifier, binding["direction"], binding["pin"])
                width, offset = binding["width"], binding["offset"]
                if key not in widths or key in seen or width != widths[key] or type(offset) is not int or offset < 0:
                    raise ValueError(f"Invalid splitter binding for {key}")
                seen.add(key)
                for index in range(width):
                    offsets[offset + index].append((key, index))
            if seen != {key for key in widths if key[0] == identifier}:
                raise ValueError(f"Splitter {identifier} has incomplete bit bindings")
            for offset, nodes in offsets.items():
                directions = [node[0][1] for node in nodes]
                if directions.count("input") != 1 or directions.count("output") < 1:
                    raise ValueError(f"Splitter {identifier} bit {offset} lacks a unique input or output")
                for node in nodes[1:]:
                    union(nodes[0], node)
        actual = defaultdict(list)
        identities = defaultdict(set)
        for node in parents:
            key, index = node
            group = find(node)
            actual[group].append(node)
            identities[expected[key][index]].add(group)
        for nodes in actual.values():
            labels = {expected[key][index] for key, index in nodes}
            if len(labels) != 1:
                errors.append({"code": "bit_connection_mismatch", "bits": sorted(labels, key=str),
                               "message": "Native wiring or splitter offsets join distinct bit identities"})
            drivers = [(key, index) for key, index in nodes
                       if key[1] == "output" and instances[key[0]]["type"] != "Splitter"]
            if len(drivers) != 1:
                errors.append({"code": "bit_driver_count", "drivers": len(drivers),
                               "message": "A native bit must have exactly one non-splitter driver"})
            for key, index in drivers:
                item, bit = instances[key[0]], expected[key][index]
                if item["type"] == "Const":
                    value = int(item["attributes"]["Value"], 16)
                    if bit != str((value >> index) & 1):
                        errors.append({"code": "bit_constant_mismatch", "instance_id": key[0], "bit_index": index,
                                       "message": "Constant value differs from the expected bit identity"})
                elif type(bit) is str:
                    errors.append({"code": "bit_constant_driver", "instance_id": key[0],
                                   "message": "A constant bit has a nonconstant native driver"})
        # Independent constant sources may have equal values without being
        # electrically joined. Ordinary design bits must form one partition.
        for bit, groups in identities.items():
            if type(bit) is int and len(groups) != 1:
                errors.append({"code": "bit_disconnected", "bit": bit, "groups": len(groups),
                               "message": "Occurrences of one design bit are electrically disconnected"})
        return {"valid": not errors, "diagnostics": errors[:32],
                "pin_bits": len(parents), "electrical_bits": len(actual)}
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return {"valid": False, "diagnostics": [{"code": "invalid_bit_topology", "message": str(error)}]}
