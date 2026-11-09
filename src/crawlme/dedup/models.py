"""Validated grouping output from deduplication."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    members: list[str] = Field(min_length=1)
    overview: str = Field(min_length=1, max_length=4000)


class Grouping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[Group]
    singletons: list[str]

    @field_validator("groups")
    @classmethod
    def duplicate_groups(cls, groups: list[Group]) -> list[Group]:
        if any(len(group.members) < 2 for group in groups):
            raise ValueError("duplicate groups require at least two members; use singletons for unmerged records")
        return groups
