"""Validated grouping output from deduplication."""

from pydantic import BaseModel, ConfigDict, Field


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    members: list[str] = Field(min_length=1)
    overview: str = Field(min_length=1, max_length=4000)


class Grouping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[Group]
