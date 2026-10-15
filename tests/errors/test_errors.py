import pytest

from crawlme.analysis import AnalysisError
from crawlme.errors import Action, Error, ErrorContext, ErrorPolicy, Kind


def test_defaults_are_per_instance():
    first = AnalysisError("invalid request", code="invalid_request", kind=Kind.INVALID_INPUT, details={"page": "first"})
    second = AnalysisError("invalid response")
    policy = ErrorPolicy()
    context = ErrorContext("analysis", 1, True, True)

    assert policy.decide(first, context).action == Action.SKIP
    assert policy.decide(second, context).action == Action.RETRY
    assert first.source == second.source == "analysis"
    assert first.code == "invalid_request" and second.code == "invalid_response"
    assert not second.details


@pytest.mark.parametrize("values", [{}, {"source": "fetch"}, {"code": "failed"}, {"source": "", "code": "failed"}])
def test_unattributed_error_rejected(values):
    with pytest.raises(ValueError, match="source and code"):
        Error("failure", **values)


def test_explicit_unknown_kind():
    error = AnalysisError("unexpected response", kind=Kind.UNKNOWN)
    context = ErrorContext("analysis", 1, True, True)
    assert ErrorPolicy().decide(error, context).action == Action.ABORT
