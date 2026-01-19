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
