"""
Minimal JSON Schema validator for tool arguments and outputs.

Supports the subset CFA-SHOC needs, with no third-party dependency:
``type`` (string, integer, number, boolean, object, array, null, or a list of these),
``required``, ``properties``, ``additionalProperties`` (bool), ``enum``, ``maxLength``,
``minLength``, ``minimum``, ``maximum``, ``items``, ``maxItems``.
Unknown keywords are ignored. A missing or empty schema accepts everything.
"""

from typing import Any, Dict, List, Optional

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(instance: Any, schema: Optional[Dict[str, Any]], path: str = "$") -> List[str]:
    """Validate `instance` against `schema`. Returns a list of errors; [] means valid."""
    if not schema:
        return []
    errors: List[str] = []

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        unknown = [t for t in types if t not in _TYPE_CHECKS]
        if unknown:
            return [f"{path}: unsupported schema type {unknown}"]
        if not any(_TYPE_CHECKS[t](instance) for t in types):
            return [f"{path}: expected {expected}, got {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate(value, properties[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")

    if isinstance(instance, list):
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))

    return errors
