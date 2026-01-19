"""Tests for render.py module."""

from datetime import datetime

import pytest
from ciwatch_mcp.models import (
    BuildInfo,
    FailureClassification,
    ScanResult,
    TestFailure,
)
from ciwatch_mcp.render import render_daily_findings, render_standup_summary


@pytest.fixture
def sample_scan_result():
    """Create a sample ScanResult for testing."""
    build_info = BuildInfo(
        build_number="12345",
        build_url="https://buildkite.com/vllm/ci/builds/12345",
        pipeline="vllm/ci",
        branch="main",
        commit="abc123def456",
        state="failed",
        created_at=datetime(2026, 1, 19, 10, 0, 0),
        finished_at=datetime(2026, 1, 19, 11, 0, 0),
    )

    failures = [
        FailureClassification(
            failure_key="key1",
            test_failure=TestFailure(
                test_name="tests/test_foo.py::test_bar",
                job_name="GPU Tests",
                error_message="AssertionError: expected 5, got 3",
            ),
            category="NEW_REGRESSION",
            confidence=0.5,
            reason="New failure with no known pattern",
        ),
        FailureClassification(
            failure_key="key2",
            test_failure=TestFailure(
                test_name="tests/test_baz.py::test_qux",
                job_name="CPU Tests",
                error_message="timeout after 30s",
            ),
            category="INFRA_SUSPECTED",
            confidence=0.7,
            reason="Infrastructure issue detected: timeout detected",
        ),
    ]

    return ScanResult(
        build_info=build_info,
        total_jobs=50,
        failed_jobs=5,
        failures=failures,
        scan_timestamp=datetime(2026, 1, 19, 12, 0, 0),
    )


def test_render_daily_findings_format(sample_scan_result):
    """Test daily findings markdown rendering."""
    md = render_daily_findings(sample_scan_result)

    # Check header
    assert "# Daily Findings - 2026-01-19" in md

    # Check summary section
    assert "## Summary" in md
    assert "[12345](https://buildkite.com/vllm/ci/builds/12345)" in md
    assert "**Branch**: main" in md
    assert "**Commit**: `abc123de`" in md
    assert "**Total Jobs**: 50, **Failed**: 5" in md
    assert "**Unique Failures**: 2" in md

    # Check categories
    assert "## Failures by Category" in md
    assert "### NEW_REGRESSION (1 failures)" in md
    assert "### INFRA_SUSPECTED (1 failures)" in md

    # Check failure details
    assert "tests/test_foo.py::test_bar" in md
    assert "GPU Tests" in md
    assert "AssertionError: expected 5, got 3" in md
    assert "Confidence: 50%" in md


def test_render_standup_summary_format(sample_scan_result):
    """Test standup summary rendering."""
    summary = render_standup_summary(sample_scan_result)

    # Check it's concise (should be 1-3 lines)
    assert len(summary.split("\n")) <= 3

    # Check key information is present
    assert "[12345](https://buildkite.com/vllm/ci/builds/12345)" in summary
    assert "FAILED" in summary
    assert "2 unique failures" in summary
    assert "1 NEW_REGRESSION" in summary
    assert "1 INFRA_SUSPECTED" in summary


def test_render_standup_summary_with_new_regressions(sample_scan_result):
    """Test standup summary includes NEW_REGRESSION test names."""
    summary = render_standup_summary(sample_scan_result)

    # Should include the test name (just the function part)
    assert "test_bar" in summary


def test_render_daily_findings_with_owner(sample_scan_result):
    """Test daily findings includes owner information."""
    # Add owner to first failure
    sample_scan_result.failures[0].owner = "alice@example.com"
    sample_scan_result.failures[0].owner_confidence = 0.9

    md = render_daily_findings(sample_scan_result)

    assert "Owner: alice@example.com (confidence: 90%)" in md


def test_render_daily_findings_empty_failures():
    """Test rendering with no failures."""
    build_info = BuildInfo(
        build_number="12345",
        build_url="https://buildkite.com/vllm/ci/builds/12345",
        pipeline="vllm/ci",
        branch="main",
        commit="abc123",
        state="passed",
        created_at=datetime.now(),
    )

    result = ScanResult(
        build_info=build_info,
        total_jobs=50,
        failed_jobs=0,
        failures=[],
        scan_timestamp=datetime.now(),
    )

    md = render_daily_findings(result)

    assert "**Unique Failures**: 0" in md
    assert "## Failures by Category" in md


def test_render_standup_summary_passed_build():
    """Test standup summary for a passed build."""
    build_info = BuildInfo(
        build_number="12345",
        build_url="https://buildkite.com/vllm/ci/builds/12345",
        pipeline="vllm/ci",
        branch="main",
        commit="abc123",
        state="passed",
        created_at=datetime.now(),
    )

    result = ScanResult(
        build_info=build_info,
        total_jobs=50,
        failed_jobs=0,
        failures=[],
        scan_timestamp=datetime.now(),
    )

    summary = render_standup_summary(result)

    assert "PASSED" in summary
    assert "0 unique failures" in summary
