"""Compile supported JSON Schema objects into strict Pydantic input models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict[str, Any],
    "array": list[Any],
}


def model_from_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    if schema.get("type") != "object":
        raise ValueError(f"{name} input schema must be an object")
    properties = schema.get("properties", {})
    if not properties:
        return type(name, (EmptyInput,), {})
    required = set(schema.get("required", []))
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_schema in properties.items():
        annotation = _annotation(f"{name}_{field_name}", field_schema)
        if field_name not in required:
            annotation = annotation | None
        default: Any = ... if field_name in required else field_schema.get("default", None)
        fields[field_name] = (
            annotation,
            Field(default, description=field_schema.get("description", "")),
        )
    return create_model(
        name,
        __config__=ConfigDict(
            extra="forbid" if schema.get("additionalProperties", True) is False else "allow"
        ),
        **fields,
    )


def _annotation(name: str, schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        values = tuple(schema["enum"])
        return Literal.__getitem__(values)
    kind = schema.get("type", "string")
    if kind == "object" and "properties" in schema:
        return model_from_schema(name, schema)
    if kind == "array":
        return list[_annotation(f"{name}_item", schema.get("items", {}))]
    return _TYPE_MAP.get(kind, Any)
