"""The analysis stage: one LLM call per fetched page.

Produces the judgment the user consumes (classification, relevance,
summary) plus the feedback signals the steering half of the feedback
loop turns into guidance.  Independent of steering: replay and the
benchmark judge use the analyzer directly, never through the steering
facade.
"""

from crawlme.analyzer.page_analyzer import Analyzer, PageAnalyzer

__all__ = ["Analyzer", "PageAnalyzer"]
