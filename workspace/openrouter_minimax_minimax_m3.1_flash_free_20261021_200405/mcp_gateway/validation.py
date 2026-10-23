"""Validation of tool call arguments against the upstream JSON Schema.

The gateway validates arguments *before* forwarding them upstream so that
invalid requests fail fast with a precise, machine-readable error instead of
an opaque upstream failure.

Reported error categories:

``missing``
    Required properties that were not supplied.
``type_mismatch``
    A property whose value has the wrong JSON type.
``unexpected_fields``
    Properties not described by the schema when the schema forbids extras.
``enum_mismatch``
    A value outside the set of allowed enum values.
"""

from __future__ import annotations

from typing import Any

#: JSON Schema type names mapped onto the Python types they accept.
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    allowed = _TYPE_CHECKS.get(expected)
    if allowed is None:
        return True
    if expected in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _resolve_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` pointers within the schema (bounded depth)."""
    seen = 0
    current = schema
    while isinstance(current, dict) and "$ref" in current and seen < 10:
        ref = current["$ref"]
        seen += 1
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return schema
        node: Any = schema
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                return schema
            node = node[part]
        if not isinstance(node, dict):
            return schema
        current = node
    return current


def _validate_node(
    schema: Any, value: Any, path: str, errors: dict[str, list[str]]
) -> None:
    if not isinstance(schema, dict):
        return
    schema = _resolve_schema(schema)

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not _type_matches(expected_type, value):
            errors["type_mismatch"].append(path)
            return
    elif isinstance(expected_type, list):
        if not any(_type_matches(item, value) for item in expected_type if isinstance(item, str)):
            errors["type_mismatch"].append(path)
            return

    if "enum" in schema and isinstance(schema["enum"], list):
        if value not in schema["enum"]:
            errors["enum_mismatch"].append(path)

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []

        for name in required:
            if isinstance(name, str) and name not in value:
                errors["missing"].append(_join(path, name))

        for name, item in value.items():
            if name in properties:
                _validate_node(properties[name], item, _join(path, name), errors)
            elif schema.get("additionalProperties") is False:
                errors["unexpected_fields"].append(_join(path, name))

        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for name, item in value.items():
                if name not in properties:
                    _validate_node(additional, item, _join(path, name), errors)

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_node(items, item, f"{path}[{index}]", errors)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def validate_arguments(
    schema: dict[str, Any] | None, arguments: Any
) -> dict[str, list[str]]:
    """Validate ``arguments`` against ``schema``.

    Returns a mapping of error category to the list of offending paths.  An
    empty mapping means the arguments are valid.
    """
    errors: dict[str, list[str]] = {
        "missing": [],
        "type_mismatch": [],
        "unexpected_fields": [],
        "enum_mismatch": [],
    }
    if not isinstance(schema, dict) or not schema:
        return errors
    if not isinstance(arguments, dict):
        errors["type_mismatch"].append("")
        return errors
    _validate_node(schema, arguments, "", errors)
    return {key: value for key, value in errors.items() if value}
