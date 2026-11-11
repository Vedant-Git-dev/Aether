"""The registration-time tool model.

A tool is more than a JSON schema: it has an identity, a capability it
belongs to, one intent inside that capability, a one-line summary for the
prompt index, a declared risk, and the backends that can serve it. Those
fields are what make visibility, duplicate detection and the executor gate
possible. The provider wire format is a projection (`WireSpec`); the model
never sees `capability`, `intent`, `risk` or `backends`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from ..llm.types import WireSpec, with_plain_param

Risk = Literal["read", "write", "external"]

MAX_SUMMARY = 90


def _py_type(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "string":
        return str
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "array":
        return list[Any]
    if kind == "object":
        return dict[str, Any]
    return Any


def model_from_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """A validation model for a JSON Schema that came from outside.

    The wire schema stays the source of truth for descriptions and
    constraints; this model only gives the executor one validated shape for
    the fields it can type. Unknown or nested shapes become ``Any`` rather
    than rejecting a call the backend would have accepted.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    fields: dict[str, Any] = {}
    for key, prop in properties.items():
        if key == "_plain" or not key.isidentifier():
            # the wire schema still carries it; a generated model only
            # types the fields a Python keyword can name
            continue
        py = _py_type(prop if isinstance(prop, dict) else {})
        if key in required:
            fields[key] = (py, ...)
        else:
            fields[key] = (py | None, None)
    return create_model(
        f"{name.replace('.', '_').replace('-', '_')}_input",
        __config__=ConfigDict(extra="allow"),
        **fields,
    )


@dataclass(frozen=True)
class ToolSpec:
    """One tool as the registry knows it."""

    id: str  # "email.send"
    capability: str  # "email"
    intent: str  # "send"
    summary: str  # one line for the prompt index, at most 90 chars
    description: str
    risk: Risk = "read"
    input_model: type[BaseModel] | None = None
    # the wire schema the provider sees. Required for schemas that carry
    # descriptions and constraints a generated model cannot reproduce.
    raw_schema: dict[str, Any] | None = None
    backends: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)
    core: bool = False

    def __post_init__(self) -> None:
        if not self.id or "." not in self.id:
            raise ValueError(f"tool id must be capability.intent, got {self.id!r}")
        if not self.capability or not self.intent:
            raise ValueError(f"tool {self.id!r} needs a capability and an intent")
        if self.risk not in ("read", "write", "external"):
            raise ValueError(f"tool {self.id!r} has an unknown risk {self.risk!r}")
        if len(self.summary) > MAX_SUMMARY:
            raise ValueError(
                f"tool {self.id!r} summary is {len(self.summary)} chars — "
                f"the limit is {MAX_SUMMARY}"
            )
        if self.input_model is None and self.raw_schema is not None:
            object.__setattr__(self, "input_model", model_from_schema(self.id, self.raw_schema))

    def index_line(self) -> str:
        """The one line the model gets before it loads the schema."""
        return f"{self.wire_name} — {self.summary}"

    @property
    def wire_name(self) -> str:
        """Provider-safe callable name: dots are not valid in function names."""
        return self.id.replace(".", "__")

    def json_schema(self) -> dict[str, Any]:
        """The wire schema, with the shared `_plain` argument."""
        if self.raw_schema is not None:
            return with_plain_param(self.raw_schema)
        if self.input_model is not None:
            return with_plain_param(self.input_model.model_json_schema())
        return with_plain_param({"type": "object", "properties": {}})

    def wire(self) -> WireSpec:
        return WireSpec(
            name=self.wire_name,
            description=self.description,
            input_schema=self.json_schema(),
            source=self.backends[0] if self.backends else "native",
        )

    def accepts(self, params: dict[str, Any]) -> bool:
        """Whether the arguments validate. A model with extra fields allowed
        never rejects a call the backend would have accepted."""
        if self.input_model is None:
            return True
        try:
            self.input_model.model_validate(params)
        except Exception:
            return False
        return True
