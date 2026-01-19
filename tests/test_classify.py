"""Tests for classify.py module."""

import pytest
from ciwatch_mcp.classify import (
    classify_failure,
    deduplicate_failures,
    validate_issue_match,
)
from ciwatch_mcp.config import (
    EXACT_MATCH_CONFIDENCE,
    FUZZY_MATCH_CONFIDENCE,
    WEAK_MATCH_CONFIDENCE,
)
from ciwatch_mcp.models import FailureClassification, TestFailure


def test_infra_classification_timeout():
    """Test infrastructure pattern detection for timeout."""
    failure = TestFailure(
        test_name="test.py::test_timeout",
        job_name="Job",
        error_message="Connection timed out after 30s",
    )

    classified = classify_failure(failure, search_github=False)

    assert classified.category == "INFRA_SUSPECTED"
    assert "timeout" in classified.reason.lower()


def test_infra_classification_oom():
    """Test infrastructure pattern detection for OOM."""
    failure = TestFailure(
        test_name="test.py::test_memory",
        job_name="Job",
        error_message="CUDA out of memory",
    )

    classified = classify_failure(failure, search_github=False)

    assert classified.category == "INFRA_SUSPECTED"
    assert "oom" in classified.reason.lower()


def test_flaky_classification():
    """Test flaky test detection."""
    failure = TestFailure(
        test_name="test.py::test_flaky_behavior",
        job_name="Job",
        error_message="AssertionError: random failure",
    )

    classified = classify_failure(failure, search_github=False)

    assert classified.category == "FLAKY_SUSPECTED"
    assert "flaky" in classified.reason.lower()


def test_new_regression_classification():
    """Test new regression classification."""
    failure = TestFailure(
        test_name="test.py::test_new_feature",
        job_name="Job",
        error_message="AssertionError: expected 5, got 3",
    )

    classified = classify_failure(failure, search_github=False)

    assert classified.category == "NEW_REGRESSION"


def test_needs_triage_classification():
    """Test needs triage for insufficient data."""
    failure = TestFailure(
        test_name="test.py::test_unknown",
        job_name="Job",
        error_message=None,  # No error message
    )

    classified = classify_failure(failure, search_github=False)

    assert classified.category == "NEEDS_HUMAN_TRIAGE"


def test_deduplicate_failures():
    """Test deduplication removes duplicates."""
    f1 = FailureClassification(
        failure_key="abc123",
        test_failure=TestFailure(test_name="test1", job_name="job1"),
        category="NEW_REGRESSION",
        confidence=0.5,
        reason="test",
    )
    f2 = FailureClassification(
        failure_key="abc123",  # same key
        test_failure=TestFailure(test_name="test1", job_name="job1"),
        category="NEW_REGRESSION",
        confidence=0.5,
        reason="test",
    )
    f3 = FailureClassification(
        failure_key="def456",  # different key
        test_failure=TestFailure(test_name="test2", job_name="job2"),
        category="INFRA_SUSPECTED",
        confidence=0.7,
        reason="test",
    )

    result = deduplicate_failures([f1, f2, f3])

    # Should only have 2 unique failures (f1 and f3)
    assert len(result) == 2
    keys = [f.failure_key for f in result]
    assert "abc123" in keys
    assert "def456" in keys


def test_classification_confidence_levels():
    """Test that different categories have appropriate confidence levels."""
    # INFRA_SUSPECTED should have 0.7 confidence
    failure_infra = TestFailure(
        test_name="test.py::test", job_name="job", error_message="timeout"
    )
    classified_infra = classify_failure(failure_infra, search_github=False)
    assert classified_infra.confidence == 0.7

    # NEW_REGRESSION should have 0.5 confidence
    failure_new = TestFailure(
        test_name="test.py::test", job_name="job", error_message="AssertionError"
    )
    classified_new = classify_failure(failure_new, search_github=False)
    assert classified_new.confidence == 0.5

    # NEEDS_HUMAN_TRIAGE should have 0.3 confidence
    failure_triage = TestFailure(
        test_name="test.py::test", job_name="job", error_message=None
    )
    classified_triage = classify_failure(failure_triage, search_github=False)
    assert classified_triage.confidence == 0.3


def test_validate_issue_match_exact_title():
    """Test exact test name match in issue title."""
    issue = {
        "title": "[CI Failure]: tests/test_foo.py::test_bar failed",
        "labels": [{"name": "ci-failure"}],
    }
    failure = TestFailure(
        test_name="tests/test_foo.py::test_bar",
        job_name="Test Job",
    )

    is_valid, confidence = validate_issue_match(issue, failure)

    assert is_valid is True
    assert confidence == EXACT_MATCH_CONFIDENCE


def test_validate_issue_match_job_name():
    """Test job name match in issue title."""
    issue = {
        "title": "[CI Failure]: Transformers Nightly Models Test",
        "labels": [{"name": "ci-failure"}],
    }
    failure = TestFailure(
        test_name="tests/test_transformers.py::test_something",
        job_name="Transformers Nightly Models Test",
    )

    is_valid, confidence = validate_issue_match(issue, failure)

    assert is_valid is True
    assert confidence == FUZZY_MATCH_CONFIDENCE


def test_validate_issue_match_no_ci_failure_label():
    """Test rejection when ci-failure label is missing."""
    issue = {
        "title": "[Doc]: Steps to run vLLM on your RTX5080 or 5090!",
        "labels": [{"name": "documentation"}],  # No ci-failure label
    }
    failure = TestFailure(
        test_name="tests/test_llm.py::test_entrypoints",
        job_name="LLM Test",
    )

    is_valid, confidence = validate_issue_match(issue, failure)

    assert is_valid is False
    assert confidence == 0.0


def test_validate_issue_match_weak_keyword():
    """Test weak match with ci-failure label but no title match."""
    issue = {
        "title": "[CI Failure]: Some other test failure",
        "labels": [{"name": "ci-failure"}],
    }
    failure = TestFailure(
        test_name="tests/test_foo.py::test_bar",
        job_name="Different Job",
    )

    is_valid, confidence = validate_issue_match(issue, failure)

    assert is_valid is True
    assert confidence == WEAK_MATCH_CONFIDENCE


def test_validate_issue_match_partial_test_name():
    """Test partial test name match (test file without function)."""
    issue = {
        "title": "[CI Failure]: tests/test_async_llm.py failures",
        "labels": [{"name": "ci-failure"}],
    }
    failure = TestFailure(
        test_name="tests/test_async_llm.py::test_load",
        job_name="Test Job",
    )

    is_valid, confidence = validate_issue_match(issue, failure)

    assert is_valid is True
    assert confidence == EXACT_MATCH_CONFIDENCE  # File path part matches
