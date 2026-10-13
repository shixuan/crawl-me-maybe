from __future__ import annotations

from crawlme.analysis import AnalysisResult, AnalyzerFeedback


def test_feedback_defaults():
    af = AnalyzerFeedback()
    assert (af.classification, af.relevance_score) == ("UNKNOWN", 0.0)
    assert (af.domain, af.url, af.title) == ("", "", "")


def test_analysis_defaults():
    ar = AnalysisResult()
    assert ar.analysis_id
    assert (ar.classification, ar.tokens_used) == ("UNKNOWN", 0)
