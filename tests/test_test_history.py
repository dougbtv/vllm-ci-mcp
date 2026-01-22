"""Integration tests for test history tracking."""

import pytest

from ciwatch_mcp.test_history import ResourceBudget, get_test_history


@pytest.fixture
def mock_bk_responses(monkeypatch):
    """Mock Buildkite CLI responses for deterministic testing."""

    def mock_build_list(pipeline, branch, limit, message_filter=None):
        """Mock build list response."""
        return [
            {
                "number": "12345",
                "web_url": "https://buildkite.com/vllm/ci/builds/12345",
                "pipeline": "vllm/ci",
                "branch": "main",
                "commit": "abc123",
                "state": "failed",
                "created_at": "2024-01-22T10:00:00Z",
                "finished_at": "2024-01-22T10:30:00Z",
            },
            {
                "number": "12344",
                "web_url": "https://buildkite.com/vllm/ci/builds/12344",
                "pipeline": "vllm/ci",
                "branch": "main",
                "commit": "def456",
                "state": "passed",
                "created_at": "2024-01-22T09:00:00Z",
                "finished_at": "2024-01-22T09:30:00Z",
            },
            {
                "number": "12343",
                "web_url": "https://buildkite.com/vllm/ci/builds/12343",
                "pipeline": "vllm/ci",
                "branch": "main",
                "commit": "ghi789",
                "state": "passed",
                "created_at": "2024-01-22T08:00:00Z",
                "finished_at": "2024-01-22T08:30:00Z",
            },
        ]

    def mock_job_list(pipeline, build_number, state=None):
        """Mock job list response."""
        if build_number == "12345":
            return [
                {
                    "id": "job-failed-1",
                    "name": "Distributed Tests (H100)",
                    "state": "failed",
                    "exit_status": 1,
                }
            ]
        elif build_number == "12344":
            return [
                {
                    "id": "job-passed-1",
                    "name": "Distributed Tests (H100)",
                    "state": "passed",
                    "exit_status": 0,
                }
            ]
        elif build_number == "12343":
            return [
                {
                    "id": "job-passed-2",
                    "name": "Distributed Tests (H100)",
                    "state": "passed",
                    "exit_status": 0,
                }
            ]
        return []

    def mock_job_log(pipeline, build_number, job_id):
        """Mock job log response."""
        if job_id == "job-failed-1":
            return """
Running pytest tests...
FAILED tests/test_dbo.py::test_bar

___________ tests/test_dbo.py::test_bar ___________

    def test_bar():
>       assert 0.590 > 0.620
E       AssertionError: accuracy too low: 0.590 < 0.620

tests/test_dbo.py:42: AssertionError
"""
        elif job_id == "job-passed-1":
            return """
Running pytest tests...
PASSED tests/test_dbo.py::test_bar
All tests passed.
"""
        elif job_id == "job-passed-2":
            return """
Running pytest tests...
PASSED tests/test_dbo.py::test_bar
All tests passed.
"""
        return ""

    # Patch the CLI functions
    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_build_list", mock_build_list)
    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_job_list", mock_job_list)
    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_job_log", mock_job_log)


@pytest.mark.asyncio
async def test_test_history_end_to_end(mock_bk_responses):
    """Test full test history collection flow."""
    result = await get_test_history(
        test_nodeid="tests/test_dbo.py::test_bar",
        branch="main",
        pipeline="vllm/ci",
        build_query=None,
        lookback_builds=5,
        job_filter=None,
        include_logs=True,
    )

    # Check structure
    assert "test_nodeid" in result
    assert result["test_nodeid"] == "tests/test_dbo.py::test_bar"
    assert "timeline" in result
    assert "assessment" in result
    assert "summary" in result

    # Check timeline (should be ordered chronologically - oldest first)
    timeline = result["timeline"]
    assert len(timeline) == 3
    assert timeline[0]["build_number"] == 12343  # Oldest
    assert timeline[1]["build_number"] == 12344
    assert timeline[2]["build_number"] == 12345  # Newest

    # Check test was found in all builds
    assert all(t["test_found"] for t in timeline)

    # Check outcomes
    assert timeline[0]["test_status"] == "pass"
    assert timeline[1]["test_status"] == "pass"
    assert timeline[2]["test_status"] == "fail"

    # Check failed build has fingerprint
    failed_entry = timeline[2]
    assert len(failed_entry["jobs"]) > 0
    failed_job = failed_entry["jobs"][0]
    assert failed_job["status"] == "fail"
    assert failed_job["fingerprint_normalized"] is not None
    assert "<NUM>" in failed_job["fingerprint_normalized"]  # Should be normalized

    # Check assessment detected regression
    assessment = result["assessment"]
    assert assessment["classification"] == "REGRESSION"
    assert assessment["confidence"] == "HIGH"
    assert assessment["transition_build"] == 12345


@pytest.mark.asyncio
async def test_test_history_with_job_filter(mock_bk_responses):
    """Test job filtering."""
    result = await get_test_history(
        test_nodeid="tests/test_dbo.py::test_bar",
        branch="main",
        pipeline="vllm/ci",
        build_query=None,
        lookback_builds=5,
        job_filter="Distributed Tests",
        include_logs=True,
    )

    assert "timeline" in result
    timeline = result["timeline"]
    # Should still find the test in filtered jobs
    assert any(t["test_found"] for t in timeline)


@pytest.mark.asyncio
async def test_test_history_exclude_logs(mock_bk_responses):
    """Test that log excerpts are excluded when include_logs=False."""
    result = await get_test_history(
        test_nodeid="tests/test_dbo.py::test_bar",
        branch="main",
        pipeline="vllm/ci",
        build_query=None,
        lookback_builds=5,
        job_filter=None,
        include_logs=False,
    )

    timeline = result["timeline"]
    # Check that failed job doesn't have log_excerpt
    failed_entry = timeline[2]
    if failed_entry["jobs"]:
        job = failed_entry["jobs"][0]
        assert "log_excerpt" not in job or job.get("log_excerpt") is None


def test_resource_budget_tracking():
    """Test resource budget enforcement."""
    budget = ResourceBudget(max_jobs_per_build=5, max_log_bytes=1000)

    # Should allow fetches within budget
    assert budget.can_fetch_log(500) is True
    budget.record_log_fetch(500)
    assert budget.total_log_bytes == 500

    # Should allow one more
    assert budget.can_fetch_log(400) is True
    budget.record_log_fetch(400)
    assert budget.total_log_bytes == 900

    # Should deny exceeding budget
    assert budget.can_fetch_log(200) is False
    assert budget.exhausted is True
    assert len(budget.warnings) > 0


def test_resource_budget_warnings():
    """Test that budget warnings are added."""
    budget = ResourceBudget(max_jobs_per_build=5, max_log_bytes=100)

    # Exhaust budget
    budget.can_fetch_log(200)

    assert budget.exhausted is True
    assert len(budget.warnings) > 0
    assert "budget exhausted" in budget.warnings[0].lower()


@pytest.mark.asyncio
async def test_test_history_not_found(monkeypatch):
    """Test handling when test is never found."""

    def mock_build_list(pipeline, branch, limit, message_filter=None):
        return [
            {
                "number": "12345",
                "web_url": "https://buildkite.com/vllm/ci/builds/12345",
                "pipeline": "vllm/ci",
                "branch": "main",
                "commit": "abc123",
                "state": "passed",
                "created_at": "2024-01-22T10:00:00Z",
                "finished_at": "2024-01-22T10:30:00Z",
            }
        ]

    def mock_job_list(pipeline, build_number, state=None):
        return [
            {
                "id": "job-1",
                "name": "Tests",
                "state": "passed",
                "exit_status": 0,
            }
        ]

    def mock_job_log(pipeline, build_number, job_id):
        return "PASSED tests/other_test.py::test_other\nAll tests passed."

    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_build_list", mock_build_list)
    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_job_list", mock_job_list)
    monkeypatch.setattr("ciwatch_mcp.test_history.run_bk_job_log", mock_job_log)

    result = await get_test_history(
        test_nodeid="tests/test_foo.py::test_missing",
        branch="main",
        pipeline="vllm/ci",
        build_query=None,
        lookback_builds=5,
        job_filter=None,
        include_logs=True,
    )

    timeline = result["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["test_found"] is False
    assert timeline[0]["test_status"] == "unknown"

    # Assessment should indicate insufficient data
    assessment = result["assessment"]
    assert assessment["classification"] == "INSUFFICIENT_DATA"
