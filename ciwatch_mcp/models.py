"""Pydantic models for vLLM CI monitoring."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BuildInfo(BaseModel):
    """Represents a Buildkite build."""

    build_number: str
    build_url: str
    pipeline: str
    branch: str
    commit: str
    state: str  # passed, failed, running, canceled
    created_at: datetime
    finished_at: Optional[datetime] = None


class JobInfo(BaseModel):
    """Represents a Buildkite job within a build."""

    job_id: str
    job_name: str
    state: str
    exit_status: Optional[int] = None
    passed: bool
    build_number: str
    soft_failed: bool = False  # True if job is allowed to fail


class TestFailure(BaseModel):
    """Represents a single test failure extracted from logs."""

    test_name: str  # e.g., "v1/test_async_llm_dp.py::test_load[ray-RequestOutputKind.DELTA]"
    job_name: str
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None
    log_snippet: Optional[str] = None


ClassificationCategory = Literal[
    "KNOWN_TRACKED",
    "INFRA_SUSPECTED",
    "FLAKY_SUSPECTED",
    "NEW_REGRESSION",
    "NEEDS_HUMAN_TRIAGE",
]


class FailureClassification(BaseModel):
    """Classified failure with metadata."""

    failure_key: str  # stable deduplication key
    test_failure: TestFailure
    category: ClassificationCategory
    github_issue: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str  # human-readable explanation
    owner: Optional[str] = None
    owner_confidence: Optional[float] = None


class ScanResult(BaseModel):
    """Complete scan result for a build."""

    build_info: BuildInfo
    total_jobs: int
    failed_jobs: int
    failures: list[FailureClassification]
    scan_timestamp: datetime


class JobTestFailure(BaseModel):
    """Test failure extracted from a specific job."""

    test_nodeid: str  # Full pytest nodeid: "tests/foo.py::test_bar[param]"
    scope: str        # Parsed scope: "tests/foo.py"
    test_name: str    # Parsed name: "test_bar[param]"
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None
    log_snippet: Optional[str] = None


class JobTestFailuresResult(BaseModel):
    """Result from get_job_test_failures tool."""

    job_info: dict
    test_failures: list[JobTestFailure]
    total_failures: int


class TestAnalyticsInfo(BaseModel):
    """Analytics info for a single test."""

    test_nodeid: str
    test_id: str  # Buildkite Analytics test ID
    test_name: str
    scope: str
    location: Optional[str] = None
    web_url: Optional[str] = None
    is_flaky: bool = False
    recently_failed: bool = False
    note: Optional[str] = None


class TestAnalyticsBulkResult(BaseModel):
    """Result from get_test_analytics_bulk tool."""

    results: list[TestAnalyticsInfo]
    not_found: list[str]
    multiple_matches: dict[str, list[dict]]
    total_checked: int
    warnings: list[str]
