"""Strict boundary values and portable canonical serialization."""

import hashlib
import json
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(min_length=1, max_length=160)]
Text = Annotated[str, Field(max_length=4000)]
Count = Annotated[int, Field(ge=0)]

SEMANTIC_SET_FIELDS = frozenset(
    {
        "known_entity_ids",
        "known_record_ids",
        "known_by",
        "participant_ids",
        "stunt_ids",
        "goal_ids",
        "acted_ids",
        "applied_trigger_ids",
        "applied_boundary_ids",
        "completed_objective_ids",
        "cause_ids",
        "observed_by",
        "key_item_ids",
    }
)


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    @model_validator(mode="after")
    def sort_semantic_sets(self) -> Self:
        for name in SEMANTIC_SET_FIELDS:
            value = getattr(self, name, None)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                object.__setattr__(self, name, sorted(value))
        return self


def canonical_value(value: object, field_name: str | None = None) -> object:
    """Normalize sets even after in-place edits; preserve ordered event and dice lists."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: canonical_value(item, key) for key, item in value.items()}
    if isinstance(value, list):
        if field_name in SEMANTIC_SET_FIELDS and all(isinstance(item, str) for item in value):
            return sorted(value)
        return [canonical_value(item) for item in value]
    return value


def canonical(value: BaseModel | object) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: BaseModel | object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ValidationIssue(Value):
    path: str
    message: str


class GameError(Exception):
    """A safe, player-visible domain error."""
