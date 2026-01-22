"""Parsing and normalization logic for Buildkite data and pytest logs."""

import hashlib
import re
from datetime import datetime
from typing import Optional

from .config import MAX_ERROR_MESSAGE_LENGTH, MAX_LOG_SNIPPET_LENGTH, MAX_STACK_TRACE_LENGTH
from .models import BuildInfo, JobInfo, TestFailure

# ANSI escape code pattern for stripping color codes
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m|\[[0-9;]*m')

# Buildkite timestamp pattern
BK_TIMESTAMP_PATTERN = re.compile(r'_bk;t=[0-9]+\x07')


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes and buildkite timestamps from text.

    Args:
        text: Text potentially containing ANSI codes

    Returns:
        Text with ANSI codes stripped
    """
    text = ANSI_ESCAPE_PATTERN.sub('', text)
    text = BK_TIMESTAMP_PATTERN.sub('', text)
    return text


# Pytest patterns
# Match both old format "FAILED test::name" and new format "test::name FAILED"
# Also match parametrized tests with brackets: test::name[param]
# Allow for ANSI codes and buildkite timestamps before/within the line
PYTEST_FAILED_PATTERN = re.compile(
    r"(?:FAILED[\s\x1b\[0-9;m]+([\w/.-]+::\S+)|([\w/.-]+::\S+)[\s\x1b\[0-9;m]+FAILED)",
    re.MULTILINE
)
PYTEST_ERROR_PATTERN = re.compile(
    r"(?:ERROR[\s\x1b\[0-9;m]+([\w/.-]+::\S+)|([\w/.-]+::\S+)[\s\x1b\[0-9;m]+ERROR)",
    re.MULTILINE
)
PYTEST_PASSED_PATTERN = re.compile(
    r"(?:PASSED[\s\x1b\[0-9;m]+([\w/.-]+::\S+)|([\w/.-]+::\S+)[\s\x1b\[0-9;m]+PASSED)",
    re.MULTILINE
)

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

    # Handle pipeline - can be dict with 'slug' or already a string
    pipeline_raw = raw.get("pipeline", "unknown")
    if isinstance(pipeline_raw, dict):
        pipeline = pipeline_raw.get("slug", "unknown")
    else:
        pipeline = pipeline_raw

    return BuildInfo(
        build_number=str(raw.get("number", raw.get("id", "unknown"))),
        build_url=raw.get("web_url", raw.get("url", "")),
        pipeline=pipeline,
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
        soft_failed=raw.get("soft_failed", False),
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
    # Pattern has 2 groups, one will be empty - take the non-empty one
    # Strip ANSI codes from captured test names
    failed_matches = PYTEST_FAILED_PATTERN.findall(log_text)
    failed_tests = [strip_ansi_codes(g1 or g2) for g1, g2 in failed_matches]

    error_matches = PYTEST_ERROR_PATTERN.findall(log_text)
    failed_tests += [strip_ansi_codes(g1 or g2) for g1, g2 in error_matches]

    # Remove duplicates while preserving order
    seen = set()
    unique_failed_tests = []
    for test in failed_tests:
        if test not in seen:
            seen.add(test)
            unique_failed_tests.append(test)

    # Fallback: Try to parse "short test summary info" section
    if not unique_failed_tests:
        stss_match = re.search(
            r"={3,}\s*short test summary info\s*={3,}(.*?)(?:={3,}|$)",
            log_text,
            re.MULTILINE | re.DOTALL
        )
        if stss_match:
            stss_section = stss_match.group(1)
            # Extract FAILED/ERROR lines from summary
            stss_failed = re.findall(r"^(?:FAILED|ERROR)\s+([\w/.-]+::\S+)", stss_section, re.MULTILINE)
            unique_failed_tests.extend(stss_failed)

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


def find_test_outcome_in_log(log_text: str, test_nodeid: str) -> dict:
    """Find specific test outcome in pytest log.

    Args:
        log_text: Raw log text from job
        test_nodeid: Full pytest nodeid (e.g., "tests/test_foo.py::test_bar")

    Returns:
        Dict with keys:
        - found: bool - whether test was found in log
        - status: str - "pass" | "fail" | "unknown"
        - error_message: Optional[str] - error message if failed
        - log_excerpt: Optional[str] - relevant log excerpt
    """
    escaped_test = re.escape(test_nodeid)

    # Check for FAILED (both formats)
    failed_pattern = rf"(?:^FAILED {escaped_test}|^{escaped_test}\s+FAILED)"
    if re.search(failed_pattern, log_text, re.MULTILINE):
        failures = extract_test_failures_from_log(log_text, "")
        matching = [f for f in failures if f.test_name == test_nodeid]
        if matching:
            return {
                "found": True,
                "status": "fail",
                "error_message": matching[0].error_message,
                "log_excerpt": matching[0].log_snippet,
            }
        # Found FAILED but couldn't extract details
        return {
            "found": True,
            "status": "fail",
            "error_message": None,
            "log_excerpt": None,
        }

    # Check for ERROR (both formats)
    error_pattern = rf"(?:^ERROR {escaped_test}|^{escaped_test}\s+ERROR)"
    if re.search(error_pattern, log_text, re.MULTILINE):
        failures = extract_test_failures_from_log(log_text, "")
        matching = [f for f in failures if f.test_name == test_nodeid]
        if matching:
            return {
                "found": True,
                "status": "fail",
                "error_message": matching[0].error_message,
                "log_excerpt": matching[0].log_snippet,
            }
        # Found ERROR but couldn't extract details
        return {
            "found": True,
            "status": "fail",
            "error_message": None,
            "log_excerpt": None,
        }

    # Check for PASSED (both formats)
    passed_pattern = rf"(?:^PASSED {escaped_test}|^{escaped_test}\s+PASSED)"
    if re.search(passed_pattern, log_text, re.MULTILINE):
        return {
            "found": True,
            "status": "pass",
            "error_message": None,
            "log_excerpt": None,
        }

    # Test not found
    return {
        "found": False,
        "status": "unknown",
        "error_message": None,
        "log_excerpt": None,
    }
