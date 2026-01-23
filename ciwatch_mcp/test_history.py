"""Test history tracking across builds."""

from typing import Optional

from .assessment import assess_test_history, generate_summary
from .buildkite_api import BuildkiteAPIError, BuildkiteClient
from .config import (
    ESTIMATED_LOG_SIZE_PER_JOB,
    MAX_BUILDS_FOR_TEST_HISTORY,
    MAX_JOBS_PER_BUILD_FOR_TEST_HISTORY,
    MAX_LOG_BYTES_FOR_TEST_HISTORY,
)
from .fingerprint import extract_fingerprint_from_log, normalize_failure_fingerprint
from .normalize import find_test_outcome_in_log, parse_build_json, parse_job_json


class ResourceBudget:
    """Tracks and enforces resource limits during test history collection."""

    def __init__(
        self,
        max_jobs_per_build: int = MAX_JOBS_PER_BUILD_FOR_TEST_HISTORY,
        max_log_bytes: int = MAX_LOG_BYTES_FOR_TEST_HISTORY,
    ):
        self.max_jobs_per_build = max_jobs_per_build
        self.max_log_bytes = max_log_bytes
        self.total_log_bytes = 0
        self.exhausted = False
        self.warnings = []

    def can_fetch_log(self, estimated_size: int = ESTIMATED_LOG_SIZE_PER_JOB) -> bool:
        """Check if we can fetch another log within budget."""
        if self.total_log_bytes + estimated_size > self.max_log_bytes:
            if not self.exhausted:
                self.warnings.append(
                    f"Log budget exhausted: {self.total_log_bytes}/{self.max_log_bytes} bytes"
                )
                self.exhausted = True
            return False
        return True

    def record_log_fetch(self, actual_size: int):
        """Record actual log size after fetch."""
        self.total_log_bytes += actual_size


class TestOutcome:
    """Represents outcome of searching for a test in a build."""

    def __init__(self):
        self.test_found = False
        self.status = "unknown"  # "pass" | "fail" | "unknown"
        self.jobs = []  # List of matching job dicts with fingerprints


async def find_test_in_job(
    test_nodeid: str,
    pipeline: str,
    build_number: str,
    job_info: dict,
    budget: ResourceBudget,
    include_logs: bool,
    client: BuildkiteClient,
) -> Optional[dict]:
    """Search for test in a single job's logs.

    Args:
        test_nodeid: Full pytest nodeid
        pipeline: Pipeline slug
        build_number: Build number
        job_info: Job info dict from bk
        budget: Resource budget tracker
        include_logs: Whether to include log excerpts in output
        client: BuildkiteClient instance

    Returns:
        Dict with job outcome info, or None if test not found or budget exhausted
    """
    if not budget.can_fetch_log():
        return None

    try:
        log_text = client.get_job_log(pipeline, build_number, job_info["id"])
        budget.record_log_fetch(len(log_text))

        outcome = find_test_outcome_in_log(log_text, test_nodeid)

        if not outcome["found"]:
            return None

        # Extract fingerprint if failed
        fingerprint_raw = None
        fingerprint_normalized = None
        if outcome["status"] == "fail":
            fingerprint_raw = extract_fingerprint_from_log(log_text, test_nodeid)
            if fingerprint_raw:
                fingerprint_normalized = fingerprint_raw  # Already normalized by extract function
            elif outcome["error_message"]:
                # Fallback to normalizing error message
                fingerprint_normalized = normalize_failure_fingerprint(outcome["error_message"])

        job_url = f"https://buildkite.com/{pipeline}/builds/{build_number}#job-{job_info['id']}"

        result = {
            "job_name": job_info.get("name", job_info.get("label", "Unknown")),
            "job_url": job_url,
            "status": outcome["status"],
            "fingerprint_raw": fingerprint_raw,
            "fingerprint_normalized": fingerprint_normalized,
        }

        if include_logs and outcome.get("log_excerpt"):
            result["log_excerpt"] = outcome["log_excerpt"]
        if outcome.get("error_message"):
            result["error_message"] = outcome["error_message"]

        return result

    except BuildkiteAPIError:
        # Log fetch failed, skip this job
        return None


async def find_test_in_build(
    test_nodeid: str,
    pipeline: str,
    build_number: str,
    job_filter: Optional[str],
    budget: ResourceBudget,
    include_logs: bool,
    client: BuildkiteClient,
) -> TestOutcome:
    """Search for test across all jobs in a build.

    Strategy:
    1. Get all jobs in build
    2. Prioritize failed jobs (most likely to contain failures)
    3. Scan until test found or budget exhausted
    4. If test not found in failed jobs, check passed jobs

    Args:
        test_nodeid: Full pytest nodeid
        pipeline: Pipeline slug
        build_number: Build number
        job_filter: Optional job name filter
        budget: Resource budget tracker
        include_logs: Whether to include log excerpts
        client: BuildkiteClient instance

    Returns:
        TestOutcome with aggregated results
    """
    outcome = TestOutcome()

    try:
        build_data = client.get_build(pipeline, build_number)
        all_jobs = build_data.get("jobs", [])
    except BuildkiteAPIError:
        # Build not accessible, return unknown
        return outcome

    # Apply job filter if provided
    if job_filter:
        all_jobs = [j for j in all_jobs if job_filter.lower() in j.get("name", "").lower()]

    # Separate failed and passed jobs
    failed_jobs = [j for j in all_jobs if j.get("state") == "failed"]
    passed_jobs = [j for j in all_jobs if j.get("state") == "passed"]

    # Limit jobs per build
    failed_jobs = failed_jobs[:budget.max_jobs_per_build]
    remaining_budget = budget.max_jobs_per_build - len(failed_jobs)
    passed_jobs = passed_jobs[:remaining_budget]

    # Search failed jobs first
    for job in failed_jobs:
        if budget.exhausted:
            break

        result = await find_test_in_job(
            test_nodeid, pipeline, build_number, job, budget, include_logs, client
        )

        if result:
            outcome.test_found = True
            outcome.jobs.append(result)

            # Update overall status (fail takes precedence)
            if result["status"] == "fail":
                outcome.status = "fail"
            elif outcome.status == "unknown":
                outcome.status = result["status"]

    # If not found yet and budget allows, search passed jobs
    if not outcome.test_found and not budget.exhausted:
        for job in passed_jobs:
            if budget.exhausted:
                break

            result = await find_test_in_job(
                test_nodeid, pipeline, build_number, job, budget, include_logs, client
            )

            if result:
                outcome.test_found = True
                outcome.jobs.append(result)

                # Update status
                if result["status"] == "fail":
                    outcome.status = "fail"
                elif outcome.status == "unknown":
                    outcome.status = result["status"]

    return outcome


async def get_test_history(
    test_nodeid: str,
    branch: str,
    pipeline: str,
    build_query: Optional[str],
    lookback_builds: int,
    job_filter: Optional[str],
    include_logs: bool,
) -> dict:
    """Track test outcome history across recent builds.

    Args:
        test_nodeid: Full pytest nodeid
        branch: Git branch to scan
        pipeline: Buildkite pipeline
        build_query: Optional message filter (e.g., "nightly")
        lookback_builds: Number of builds to scan
        job_filter: Optional job name filter
        include_logs: Include log excerpts in output

    Returns:
        Dict with timeline, assessment, and summary
    """
    # Initialize Buildkite client
    client = BuildkiteClient()

    # Initialize budget tracker
    budget = ResourceBudget()

    # Fetch builds on branch
    # NOTE: build_query parameter is deprecated and ignored to avoid timeouts
    # (message_filter uses client-side filtering which is slow)
    try:
        builds_raw = client.list_builds(
            pipeline=pipeline,
            branch=branch,
            limit=lookback_builds,
        )
    except BuildkiteAPIError as e:
        return {"error": str(e)}

    if not builds_raw:
        return {
            "error": f"No builds found on branch {branch}",
            "timeline": [],
            "assessment": {"classification": "INSUFFICIENT_DATA", "confidence": "LOW", "notes": []},
            "summary": f"No builds found for test {test_nodeid}",
        }

    # Parse and sort builds (oldest first for chronological timeline)
    builds_parsed = [parse_build_json(b) for b in builds_raw]
    builds_parsed.sort(key=lambda b: b.created_at)

    # Scan each build
    timeline = []
    for build_info in builds_parsed:
        if budget.exhausted:
            budget.warnings.append(f"Stopped scanning after {len(timeline)} builds (budget exhausted)")
            break

        outcome = await find_test_in_build(
            test_nodeid=test_nodeid,
            pipeline=pipeline,
            build_number=build_info.build_number,
            job_filter=job_filter,
            budget=budget,
            include_logs=include_logs,
            client=client,
        )

        timeline.append({
            "build_number": int(build_info.build_number),
            "build_url": build_info.build_url,
            "created_at": build_info.created_at.isoformat(),
            "commit_sha": build_info.commit,
            "test_found": outcome.test_found,
            "test_status": outcome.status,
            "jobs": outcome.jobs,
        })

    # Assess timeline
    assessment = assess_test_history(timeline)

    # Generate summary
    summary = generate_summary(test_nodeid, timeline, assessment)

    result = {
        "test_nodeid": test_nodeid,
        "timeline": timeline,
        "assessment": assessment,
        "summary": summary,
    }

    # Add budget warnings if any
    if budget.warnings:
        result["warnings"] = budget.warnings

    return result
