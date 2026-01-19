"""Parsing and normalization logic for Buildkite data and pytest logs."""

import hashlib
import re
from datetime import datetime
from typing import Optional

from .config import MAX_ERROR_MESSAGE_LENGTH, MAX_LOG_SNIPPET_LENGTH, MAX_STACK_TRACE_LENGTH
from .models import BuildInfo, JobInfo, TestFailure

# Pytest patterns
PYTEST_FAILED_PATTERN = re.compile(r"^FAILED ([\w/.-]+::\S+)", re.MULTILINE)
PYTEST_ERROR_PATTERN = re.compile(r"^ERROR ([\w/.-]+::\S+)", re.MULTILINE)

# Error signature patterns for deduplication
ERROR_SIG_PATTERNS = [
    re.compile(r"(\w+Error): (.+?)(?:\n|$)"),  # Python exceptions
    re.compile(r"AssertionError: (.+?)(?:\n|$)"),
    re.compile(r"RuntimeError: (.+?)(?:\n|$)"),
    re.compile(r"TimeoutError: (.+?)(?:\n|$)"),
]


def parse_build_json(raw: dict) -> BuildInfo:
    """Convert raw bk build JSON to BuildInfo model.

    Args:
        raw: Raw build dict from bk CLI

    Returns:
        BuildInfo model
    """
    # Handle datetime parsing
    created_at = raw.get("created_at", "")
    finished_at = raw.get("finished_at")

    # Parse ISO timestamps
    if created_at:
        created_at_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    else:
        created_at_dt = datetime.now()

    finished_at_dt = None
    if finished_at:
        finished_at_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))

    return BuildInfo(
        build_number=str(raw.get("number", raw.get("id", "unknown"))),
        build_url=raw.get("web_url", raw.get("url", "")),
        pipeline=raw.get("pipeline", {}).get("slug", "unknown"),
        branch=raw.get("branch", "unknown"),
        commit=raw.get("commit", "unknown"),
        state=raw.get("state", "unknown"),
        created_at=created_at_dt,
        finished_at=finished_at_dt,
    )


def parse_job_json(raw: dict, build_number: str) -> JobInfo:
    """Convert raw bk job JSON to JobInfo model.

    Args:
        raw: Raw job dict from bk CLI
        build_number: Build number this job belongs to

    Returns:
        JobInfo model
    """
    state = raw.get("state", "unknown")
    passed = state == "passed"

    return JobInfo(
        job_id=raw.get("id", "unknown"),
        job_name=raw.get("name", raw.get("label", "Unknown Job")),
        state=state,
        exit_status=raw.get("exit_status"),
        passed=passed,
        build_number=build_number,
    )


def extract_test_failures_from_log(log_text: str, job_name: str) -> list[TestFailure]:
    """Parse pytest output to extract individual test failures.

    Strategy:
    1. Find FAILED/ERROR test names
    2. Extract error messages from failure sections
    3. If pytest parsing fails, return job-level failure

    Args:
        log_text: Raw log text from job
        job_name: Name of the job

    Returns:
        List of TestFailure models
    """
    failures = []

    # Find all FAILED and ERROR test names
    failed_tests = PYTEST_FAILED_PATTERN.findall(log_text)
    failed_tests += PYTEST_ERROR_PATTERN.findall(log_text)

    # Remove duplicates while preserving order
    seen = set()
    unique_failed_tests = []
    for test in failed_tests:
        if test not in seen:
            seen.add(test)
            unique_failed_tests.append(test)

    if not unique_failed_tests:
        # No pytest output detected, return job-level failure
        return [
            TestFailure(
                test_name=job_name,
                job_name=job_name,
                error_message="Job failed without pytest test names",
                log_snippet=log_text[-MAX_LOG_SNIPPET_LENGTH:],  # last N chars
            )
        ]

    # Extract details for each test
    for test_name in unique_failed_tests:
        failure = TestFailure(test_name=test_name, job_name=job_name)

        # Try to find error section for this test
        # Look for section delimited by underscores
        escaped_test = re.escape(test_name)
        test_section_match = re.search(
            rf"_{10,}\s+{escaped_test}\s+_{10,}(.*?)(?=_{10,}|\Z)",
            log_text,
            re.DOTALL,
        )

        if test_section_match:
            section_text = test_section_match.group(1)

            # Extract error message (last exception line)
            for pattern in ERROR_SIG_PATTERNS:
                match = pattern.search(section_text)
                if match:
                    error_msg = match.group(0).strip()
                    failure.error_message = error_msg[:MAX_ERROR_MESSAGE_LENGTH]
                    break

            # Extract stack trace (first N chars of section)
            failure.stack_trace = section_text[:MAX_STACK_TRACE_LENGTH]
            failure.log_snippet = section_text[:MAX_LOG_SNIPPET_LENGTH]
        else:
            # No section found, try to extract error from surrounding context
            # Look for the test name in the log and grab context
            test_context_match = re.search(
                rf"{escaped_test}.*?(?:\n.*?){{0,10}}",
                log_text,
                re.DOTALL,
            )
            if test_context_match:
                context = test_context_match.group(0)
                failure.log_snippet = context[:MAX_LOG_SNIPPET_LENGTH]

        failures.append(failure)

    return failures


def generate_failure_key(failure: TestFailure) -> str:
    """Generate stable deduplication key for a failure.

    Key components:
    - job_name (normalized)
    - test_name
    - error signature (exception type + first line of message)

    Args:
        failure: TestFailure model

    Returns:
        16-character hex string (SHA256 hash truncated)
    """
    components = [
        failure.job_name.lower().replace(" ", "_"),
        failure.test_name,
    ]

    if failure.error_message:
        # Extract just exception type and first meaningful line
        error_sig = failure.error_message.split("\n")[0][:100]
        components.append(error_sig)

    key_string = "::".join(components)
    return hashlib.sha256(key_string.encode()).hexdigest()[:16]
