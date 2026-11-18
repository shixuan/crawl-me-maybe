"""Goal models and extraction specifications, independent of LLM enhancement."""

from crawlme.goals.models import Goal
from crawlme.goals.spec import spec_fields, spec_time_field, spec_version

__all__ = ["Goal", "spec_fields", "spec_time_field", "spec_version"]
