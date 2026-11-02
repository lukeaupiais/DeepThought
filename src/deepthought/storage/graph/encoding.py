"""Lossless mapping of portable JSON properties to Neo4j property values."""

import json
import math


def encode_properties(properties):
    """Keep queryable scalar fields native; preserve nested JSON and explicit nulls."""
    if not isinstance(properties, dict) or any(
        not isinstance(k, str) or k.startswith("_dt_") for k in properties
    ):
        raise ValueError(
            "Property keys must be strings outside reserved _dt_ namespace"
        )
    json.dumps(properties, allow_nan=False)
    result, encoded, nulls = {}, [], []
    for key in sorted(properties):
        value = properties[key]
        if value is None:
            nulls.append(key)
        elif type(value) in (str, bool, int, float):
            if type(value) is int and not -(2**63) <= value < 2**63:
                encoded.append(key)
                result[key] = json.dumps(value)
            elif type(value) is float and not math.isfinite(value):
                raise ValueError("Properties must be finite JSON")
            else:
                result[key] = value
        elif (
            isinstance(value, list)
            and value
            and all(
                type(x) is type(value[0])
                and type(x) in (str, bool, int, float)
                and (type(x) is not int or -(2**63) <= x < 2**63)
                for x in value
            )
        ):
            result[key] = value
        else:
            encoded.append(key)
            result[key] = json.dumps(value, sort_keys=True, allow_nan=False)
    if encoded:
        result["_dt_json_fields"] = encoded
    if nulls:
        result["_dt_null_fields"] = nulls
    return result


def decode_properties(properties):
    """Reconstruct the original portable properties, including absent-vs-null."""
    result = dict(properties)
    result.pop("_dt_edge_id", None)
    for key in result.pop("_dt_json_fields", []):
        result[key] = json.loads(result[key])
    for key in result.pop("_dt_null_fields", []):
        result[key] = None
    return result
