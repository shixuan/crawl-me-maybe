from dataclasses import replace

import pytest

from crawlme.digest import FetchError
from crawlme.errors import Action, Error, ErrorContext, ErrorPolicy, Kind, Scope
from crawlme.llm import RemoteError, TokenBudgetError


def context(**values):
    return replace(ErrorContext("fetch", 1, True, True), **values)


@pytest.mark.parametrize("kind", [Kind.UNAUTHENTICATED, Kind.FORBIDDEN, Kind.RESOURCE_EXHAUSTED])
def test_account_error_aborts(kind):
    assert ErrorPolicy().decide(RemoteError("account failed", kind=kind), context()).action == Action.ABORT


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_page_rejection_skips(status):
    error = FetchError.from_http(status, url="https://example.test")
    assert ErrorPolicy().decide(error, context()).action == Action.SKIP


def test_budget_is_normal_stop():
    result = ErrorPolicy().decide(TokenBudgetError("limit reached"), context(operation="analysis"))
    assert result.action == Action.SKIP and result.reason == "limit_reached"


@pytest.mark.parametrize(
    "kind", [Kind.TIMEOUT, Kind.UNAVAILABLE, Kind.RATE_LIMITED, Kind.INVALID_OUTPUT, Kind.CONFLICT]
)
def test_retry_then_skip(kind):
    error = Error("try again", source="component", code="local", kind=kind)
    policy = ErrorPolicy()
    decisions = [policy.decide(error, context(attempt=attempt)) for attempt in range(1, 4)]
    assert [d.action for d in decisions] == [Action.RETRY, Action.RETRY, Action.SKIP]
    assert [d.delay for d in decisions] == [1, 2, 0]


@pytest.mark.parametrize("operation", ["dedup", "ranking"])
def test_repair_is_not_repeated(operation):
    error = Error("invalid output after repair", source=operation, code="invalid_json", kind=Kind.INVALID_OUTPUT)
    assert ErrorPolicy().decide(error, context(operation=operation, retry_invalid_output=False)).action == Action.SKIP


@pytest.mark.parametrize("optional", [True, False])
def test_unknown_always_aborts(optional):
    error = Error("broken", source="component", code="unknown")
    assert ErrorPolicy().decide(error, context(optional=optional)).action == Action.ABORT


@pytest.mark.parametrize("retry_after", [8.0, 60.0, -1.0, float("nan"), float("inf"), True, "8"])
def test_retry_after(retry_after):
    error = Error(
        "limited", source="fetch", code="limited", kind=Kind.RATE_LIMITED, details={"retry_after": retry_after}
    )
    decision = ErrorPolicy(max_delay=4).decide(error, context())
    assert decision.delay == (retry_after if retry_after in (8.0, 60.0) else 1)


def test_nonrepeatable_aborts():
    error = Error("write failed", source="persist", code="failed", kind=Kind.UNAVAILABLE)
    assert ErrorPolicy().decide(error, context(optional=False, repeatable=False)).action == Action.ABORT


@pytest.mark.parametrize("source", ["fetch", "platform", "new_component"])
@pytest.mark.parametrize("scope,action", [(Scope.OPERATION, Action.SKIP), (Scope.DEPENDENCY, Action.ABORT)])
def test_scope_over_component(source, scope, action):
    error = Error("missing", source=source, code="arbitrary_code", kind=Kind.NOT_FOUND, scope=scope)
    assert ErrorPolicy().decide(error, context(operation="unrelated")).action == action


def test_repair_context_over_name():
    error = Error("invalid", source="new_component", code="arbitrary_code", kind=Kind.INVALID_OUTPUT)
    policy = ErrorPolicy()
    assert policy.decide(error, context(operation="dedup")).action == Action.RETRY
    assert policy.decide(error, context(operation="new_operation", retry_invalid_output=False)).action == Action.SKIP


def test_missing_session_is_shared(tmp_path):
    from crawlme.digest.fetcher.browser import _load_storage_state

    with pytest.raises(FetchError) as caught:
        _load_storage_state(str(tmp_path / "missing.json"))
    assert caught.value.scope == Scope.DEPENDENCY
    assert ErrorPolicy().decide(caught.value, context()).action == Action.ABORT


@pytest.mark.parametrize("kind", [Kind.TIMEOUT, Kind.UNAVAILABLE, Kind.RATE_LIMITED, Kind.CONFLICT])
@pytest.mark.parametrize("scope", [Scope.OPERATION, Scope.DEPENDENCY])
def test_service_retry_exhaustion(kind, scope):
    failure = Error("unavailable", source="any_component", code="any_code", kind=kind, scope=scope)
    policy = ErrorPolicy()
    decisions = [policy.decide(failure, context(attempt=i)) for i in range(1, 4)]
    final = Action.ABORT if scope == Scope.DEPENDENCY else Action.SKIP
    assert [d.action for d in decisions] == [Action.RETRY, Action.RETRY, final]


def test_bad_output_stays_local():
    failure = Error("invalid reply", source="component", code="invalid", kind=Kind.INVALID_OUTPUT)
    assert ErrorPolicy().decide(failure, context(attempt=3)).action == Action.SKIP
